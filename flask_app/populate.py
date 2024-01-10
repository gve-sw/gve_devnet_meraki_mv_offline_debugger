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

import os
from pprint import pprint
from rich.console import Console
from rich.panel import Panel
from dotenv import load_dotenv

import meraki

import config
import db
import threading

# Rich Console Instance
console = Console()

# Load in Environment Variables
load_dotenv()
MERAKI_API_KEY = os.getenv('MERAKI_API_KEY')


# connect to Meraki dashboard
dashboard = meraki.DashboardAPI(MERAKI_API_KEY, suppress_logging=True, maximum_retries=25)


def thread_wrapper(net, mac_to_serial, serial_to_model, serial_to_status):
    """
    Thread for processing the topology of each network (devices, links, connections, etc.).
    If a valid topological connection is found, add it to the DB.
    :param net: Current Network
    :param mac_to_serial: Org Wide Device Mac to Serial Mapping
    :param serial_to_model: Org Wide Device Serials to Meraki Model Mapping
    :param serial_to_status: Org Wide Device Serial to Device Status Mapping
    :return: Add Valid Mappings to DB (no return)
    """
    # connect to database
    conn = db.create_connection("sqlite.db")

    net_id = net['id']

    # grab the network topology from Meraki dashboard to determine which devices are connected to each other
    try:
        topology = dashboard.networks.getNetworkTopologyLinkLayer(net_id)
    except Exception as e:
        # All exceptions (invalid network, etc. are skipped)
        return

    # Build Derived ID to MAC Dictionary
    derived_id_to_mac = {}
    for node in topology['nodes']:
        if 'mac' in node:
            derived_id_to_mac[node['derivedId']] = node['mac']

    links = topology["links"]

    connections = []
    for link in links:
        serials = []

        ends = link["ends"]
        for end in ends:
            if end["node"]["type"] == "device":
                device = end["device"]
                serial = device["serial"]
                serials.append({"serial": serial})
            elif end["node"]["type"] == "discovered":
                # Handle cross network topology case

                # Check nodes list to see if there's an entry based on derivedId
                derivedId = end["node"]['derivedId']

                if derivedId in derived_id_to_mac:
                    mac = derived_id_to_mac[derivedId]
                    if mac in mac_to_serial:
                        serial = mac_to_serial[mac]
                        serials.append({"serial": serial})

        connections.append(serials)

    processed_connections = []
    for connection in connections:
        for node in connection:
            if node["serial"] in serial_to_model:
                model = serial_to_model[node['serial']]

                if "MV" in model:
                    node["type"] = "camera"
                elif "MS" in model:
                    node["type"] = "switch"
                elif "MX" in model:
                    node["type"] = "router"
                else:
                    node["type"] = "N/A"
            else:
                node["type"] = "N/A"

            # Get status to determine what to initialize database
            device_status = serial_to_status[node["serial"]]
            if device_status == "online" or device_status == "alerting":
                node["status"] = "up"
            else:
                node["status"] = "down"

        if len(connection) > 1:
            device_types = {connection[0]["type"], connection[1]["type"]}

            if "camera" in device_types and "switch" in device_types:
                processed_connections.append(connection)
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
                processed_connections.append(connection)
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

    # Display Results of Processing Network at the end of the thread
    console.print("\nProcessed Network: [blue]'{}'[/]".format(net['name']))
    console.print(f"Processed the following connection(s): {processed_connections}")


def main():
    # Iterate through every org, every network, build topology information
    console.print(Panel.fit("Building Topology Table:"))

    orgs = dashboard.organizations.getOrganizations()
    for org in orgs:
        console.print(Panel.fit(f"Processing Org: {org['name']}"))
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

        # Get Org Devices, build mac to serial dictionary, serial to model dictionary (chain of dictionaries!)
        if len(config.TARGET_NETWORKS) > 0:
            devices = dashboard.organizations.getOrganizationDevices(org_id, total_pages='all', networkIds=networks_ids)
        else:
            devices = dashboard.organizations.getOrganizationDevices(org_id, total_pages='all')

        mac_to_serial = {}
        serial_to_model = {}
        for device in devices:
            mac_to_serial[device["mac"]] = device["serial"]
            serial_to_model[device['serial']] = device['model']

        # Get Org Device Statuses, build serial to status dictionary
        if len(config.TARGET_NETWORKS) > 0:
            device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all', networkIds=networks_ids)
        else:
            device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all')

        serial_to_status = {}
        for status in device_statuses:
            serial_to_status[status["serial"]] = status["status"]

        console.print(
            f"\nProcessing the following networks: {[network['name'] for network in networks]}")
        console.print(f"Total Networks to process: {len(networks)}")

        threads = []
        for net in networks:
            # Spawn a background thread
            thread = threading.Thread(target=thread_wrapper,
                                      args=(net, mac_to_serial, serial_to_model, serial_to_status, ))
            threads.append(thread)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for all threads to finish
        for t in threads:
            t.join()

    # connect to database
    conn = db.create_connection("sqlite.db")

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
