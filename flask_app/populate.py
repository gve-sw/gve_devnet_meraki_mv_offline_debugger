#!/usr/bin/env python3
"""
Copyright (c) 2023 Cisco and/or its affiliates.
This software is licensed to you under the terms of the Cisco Sample
Code License, Version 1.1 (the "License"). You may obtain a copy of the
License at
https://developer.cisco.com/docs/licenses
All use of the material herein must be in accordance with the terms of
the License. All rights not expressly granted by the License are
reserved. Unless required by applicable law or agreed to separately in
writing, software distributed under the License is distributed on an "AS
IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
or implied.
"""

__author__ = "Trevor Maco <tmaco@cisco.com>"
__copyright__ = "Copyright (c) 2023 Cisco and/or its affiliates."
__license__ = "Cisco Sample Code License, Version 1.1"

from pprint import pprint
from rich.console import Console
from rich.progress import Progress
from rich.panel import Panel

import meraki

import config
import db

# Rich Console Instance
console = Console()

# connect to Meraki dashboard
dashboard = meraki.DashboardAPI(config.MERAKI_API_KEY, suppress_logging=True, maximum_retries=25)


def main():
    # connect to database
    conn = db.create_connection("sqlite.db")

    # Iterate through every org, every network, build topology information
    console.print(Panel.fit("Building Topology Table:"))

    orgs = dashboard.organizations.getOrganizations()
    for org in orgs:
        org_id = org["id"]

        # get net id for net name in environment variables
        try:
            networks = dashboard.organizations.getOrganizationNetworks(org_id, total_pages="all")
        except meraki.APIError as e:
            continue

        # Optional: filter out the specific networks from the list
        if len(config.TARGET_NETWORKS) > 0:
            networks = [entry for entry in networks if entry['name'] in config.TARGET_NETWORKS]
            networks_ids = [entry['id'] for entry in networks if entry['name'] in config.TARGET_NETWORKS]

            # No matching networks in org found, skip
            if len(networks) == 0:
                continue

        # Get Org Devices, build mac to serial dictionary
        if len(config.TARGET_NETWORKS) > 0:
            devices = dashboard.organizations.getOrganizationDevices(org_id, total_pages='all', networkIds=networks_ids)
        else:
            devices = dashboard.organizations.getOrganizationDevices(org_id, total_pages='all')

        mac_to_serial = {}
        for device in devices:
            mac_to_serial[device["mac"]] = device["serial"]

        # Get Org Device Statuses, build serial to status dictionary
        if len(config.TARGET_NETWORKS) > 0:
            device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all', networkIds=networks_ids)
        else:
            device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all')

        serial_to_status = {}
        for status in device_statuses:
            serial_to_status[status["serial"]] = status["status"]

        console.print(
            f"\nProcessing the following org's ({org['name']}) networks: {[network['name'] for network in networks]}")

        with Progress() as progress:
            overall_progress = progress.add_task("Overall Progress", total=len(networks), transient=True)
            counter = 1

            for net in networks:
                progress.console.print(
                    "\nProcessing Network: [blue]'{}'[/] ({} of {})".format(net['name'], str(counter), len(networks)))

                net_id = net["id"]

                # grab the network topology from Meraki dashboard to determine which devices are connected to each other
                try:
                    topology = dashboard.networks.getNetworkTopologyLinkLayer(net_id)
                except Exception as e:
                    # All exceptions (invalid network, etc. are skipped)
                    continue

                links = topology["links"]
                progress.console.print(f"Found {len(links)} link(s)")

                connections = []
                for link in links:
                    serials = []

                    ends = link["ends"]
                    progress.console.print(f"Processing link ends: {ends}")
                    for end in ends:
                        if end["node"]["type"] == "device":
                            device = end["device"]
                            serial = device["serial"]
                            serials.append({"serial": serial})
                            progress.console.print(f"- Processing device with serial: {serial}")
                        elif end["node"]["type"] == "discovered":
                            # Handle cross network topology case

                            # Check nodes list to see if there's an entry based on derivedId
                            derivedId = end["node"]['derivedId']
                            for node in topology['nodes']:
                                # If there's an entry, and an associated mac for look up, retrieve device serial
                                if derivedId == node['derivedId'] and 'mac' in node:
                                    if node['mac'] in mac_to_serial:
                                        serial = mac_to_serial[node['mac']]
                                        serials.append({"serial": serial})
                                        progress.console.print(
                                            f"- Processing devices with serial (from discovery): {serial}")

                    connections.append(serials)

                progress.console.print(f"Processing {len(connections)} connection(s)")
                for connection in connections:
                    progress.console.print(f"- Processing connection: {connection}")

                    for node in connection:
                        device = dashboard.devices.getDevice(node["serial"])
                        if "MV" in device["model"]:
                            node["type"] = "camera"
                        elif "MS" in device["model"]:
                            node["type"] = "switch"
                        elif "MX" in device["model"]:
                            node["type"] = "router"
                        else:
                            node["type"] = "N/A"
                        # get status to determine what to initialize database
                        device_status = serial_to_status[node["serial"]]
                        if device_status == "online" or device_status == "alerting":
                            node["status"] = "up"
                        else:
                            node["status"] = "down"

                    if len(connection) > 1:
                        device_types = {connection[0]["type"], connection[1]["type"]}

                        if "camera" in device_types and "switch" in device_types:
                            progress.console.print(f"-- Adding device connection of type: {device_types}")

                            # check if the first connection is a switch
                            if connection[0]["type"] == "switch":
                                # check if switch already exists in the database
                                data = db.query_specific_switch(conn, connection[0]["serial"])
                                if len(data) == 0:
                                    # if switch not already in the database, add it
                                    db.add_switch(conn, connection[0]["serial"],
                                                  connection[0]["status"])

                                # now add the camera into the database
                                db.add_camera(conn, connection[1]["serial"],
                                              connection[1]["status"], connection[0]["serial"])
                            # the first connection is not a switch, so it must be a camera
                            else:
                                # check if switch already exists in the database
                                data = db.query_specific_switch(conn, connection[1]["serial"])
                                if len(data) == 0:
                                    # add switch to database
                                    db.add_switch(conn, connection[1]["serial"],
                                                  connection[1]["status"])

                                # now add the camera to the database
                                db.add_camera(conn, connection[0]["serial"],
                                              connection[0]["status"], connection[1]["serial"])
                        elif "switch" in device_types and "router" in device_types:
                            progress.console.print(f"-- Adding device connection of type: {device_types}")

                            # check if first connection is a router
                            if connection[0]["type"] == "router":
                                # check if router already exists in the database
                                data = db.query_specific_router(conn, connection[0]["serial"])
                                if len(data) == 0:
                                    # router is not in database, so it must be added
                                    db.add_router(conn, connection[0]["serial"],
                                                  connection[0]["status"])

                                # now add switch into the database
                                db.add_switch(conn, connection[1]["serial"],
                                              connection[1]["status"], connection[0]["serial"])
                            # the first connection is not a router, so it must be a switch
                            else:
                                # check if router already exists in database
                                data = db.query_specific_router(conn, connection[1]["serial"])
                                if len(data) == 0:
                                    # router is not in database and needs to be added
                                    db.add_router(conn, connection[1]["serial"],
                                                  connection[1]["status"])

                                # now add switch into database
                                db.add_switch(conn, connection[0]["serial"],
                                              connection[0]["status"], connection[1]["serial"])

                counter += 1
                progress.update(overall_progress, advance=1)

    # print the results of all the queries to all the tables
    console.print(Panel.fit("Aggregate Table Output:"))

    console.print("[green]Routers (router, status):[/]")
    pprint(db.query_all_routers(conn))
    console.print("[green]Switches (switch, router, switch status):[/]")
    pprint(db.query_all_switches(conn))
    console.print("[green]Cameras (camera, switch, camera status):[/]")
    pprint(db.query_all_cameras(conn))

    # close the database connection
    db.close_connection(conn)


if __name__ == "__main__":
    main()
