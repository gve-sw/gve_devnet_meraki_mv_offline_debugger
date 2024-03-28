#!/usr/bin/env python3
"""
Copyright (c) 2024 Cisco and/or its affiliates.
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
__copyright__ = "Copyright (c) 2024 Cisco and/or its affiliates."
__license__ = "Cisco Sample Code License, Version 1.1"

import os
from rich.console import Console
from rich.panel import Panel
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import argparse

import meraki

import config
import db

# Rich Console Instance
console = Console()

# Load in Environment Variables
load_dotenv()
MERAKI_API_KEY = os.getenv('MERAKI_API_KEY')

# connect to Meraki dashboard
dashboard = meraki.DashboardAPI(MERAKI_API_KEY, suppress_logging=True, maximum_retries=50)


def clear_stale_devices():
    """
    Background thread, remove devices still in the DB but no longer in the inventory (removed devices for ex.)
    """
    conn = db.create_connection("sqlite.db")

    # Get orgs
    try:
        orgs = dashboard.organizations.getOrganizations()
    except meraki.APIError as e:
        # If there's an issue (429, etc.), then skip this background task and try again in the next cycle
        return

    total_inventory = []
    for org in orgs:
        # Get Organization inventory
        try:
            inventory = dashboard.organizations.getOrganizationInventoryDevices(org['id'],
                                                                            productTypes=['appliance', 'camera',
                                                                                          'switch'], total_pages='all')
        except meraki.APIError as e:
            continue

        # Add only serials into org inventory
        for item in inventory:
            total_inventory.append(item["serial"])

    # Check if any cameras are no longer in the inventory
    cameras = db.query_all_cameras(conn)
    for camera in cameras:
        serial = camera[0]

        if serial not in total_inventory:
            # Remove stale item
            console.log(f'Deleted camera {serial} from DB - clean up thread')
            db.delete_camera(conn, serial)

    # Check if any switches are no longer in the inventory
    switches = db.query_all_switches(conn)
    for switch in switches:
        serial = switch[0]

        if serial not in total_inventory:
            # Remove stale item
            console.log(f'Deleted switch {serial} from DB - clean up thread')
            db.delete_switch(conn, serial)

    # Check if any routers are no longer in the inventory
    routers = db.query_all_routers(conn)
    for router in routers:
        serial = router[0]

        if serial not in total_inventory:
            # Remove stale item
            console.log(f'Deleted router {serial} from DB - clean up thread')
            db.delete_router(conn, serial)


def thread_wrapper(net: dict, mac_to_serial: dict, serial_to_model: dict, serial_to_status: dict):
    """
    Thread for processing the topology of each network (devices, links, connections, etc.).
    If a valid topological connection is found, add it to the DB.
    :param net: Current Network
    :param mac_to_serial: Org Wide Device Mac to Serial Mapping
    :param serial_to_model: Org Wide Device Serials to Meraki Model Mapping
    :param serial_to_status: Org Wide Device Serial to Device Status Mapping
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


def update_network_topology(org_id: str, net: dict):
    """
    Rerun Topology calculations on a specific network (obtain org structures, rebuild topology if necessary)
    :param org_id: Org ID
    :param net: Network object (reduced - name and ID only)
    """
    networks = dashboard.organizations.getOrganizationNetworks(org_id, total_pages="all")

    # Optional: filter out the specific networks from the list
    if len(config.TARGET_NETWORKS) > 0:
        networks = [entry for entry in networks if entry['name'] in config.TARGET_NETWORKS]
        networks_ids = [entry['id'] for entry in networks if entry['name'] in config.TARGET_NETWORKS]

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
        device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all',
                                                                                 networkIds=networks_ids)
    else:
        device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all')

    serial_to_status = {}
    for status in device_statuses:
        serial_to_status[status["serial"]] = status["status"]

    console.print(f"\nUpdating topology for network: {net['name']}")

    # Spawn a background thread to process network
    with ThreadPoolExecutor() as executor:
        future = executor.submit(thread_wrapper, net, mac_to_serial, serial_to_model, serial_to_status)


def build_full_topology():
    """
    Build full topology tables: construct camera, switch, router tables and their various connects. Warning: this can be
    time intensive!
    """
    # Iterate through every org, every network, build topology information
    orgs = dashboard.organizations.getOrganizations()
    for org in orgs:
        console.print(Panel.fit(f"Processing Org: {org['name']}"))
        org_id = org["id"]

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
            device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all',
                                                                                     networkIds=networks_ids)
        else:
            device_statuses = dashboard.organizations.getOrganizationDevicesStatuses(org_id, total_pages='all')

        serial_to_status = {}
        for status in device_statuses:
            serial_to_status[status["serial"]] = status["status"]

        console.print(
            f"\nProcessing the following networks: {[network['name'] for network in networks]}")
        console.print(f"Total Networks to process: {len(networks)}")

        max_threads = 20
        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = [executor.submit(thread_wrapper, net, mac_to_serial, serial_to_model, serial_to_status) for net in
                       networks]

    # Print the results of all the queries to all the tables
    console.print(Panel.fit("Aggregate Table Output:"))

    print_table('router')
    print_table('switch')
    print_table('camera')


def print_table(table: str):
    """
    Print contents of specified table (useful for debugging and replacing camera serials)
    :param table: Table name from CLI args (options: camera, switch, router)
    """
    # Connect to database
    conn = db.create_connection("sqlite.db")

    if table == 'camera':
        console.print("Cameras ([green]camera[/], [green]switch[/], [green]camera status[/]):")
        console.print(db.query_all_cameras(conn))
    elif table == 'switch':
        console.print("Switches ([green]switch[/], [green]router[/], [green]switch status[/]):")
        console.print(db.query_all_switches(conn))
    elif table == 'router':
        console.print("Routers ([green]router[/], [green]status[/]):")
        console.print(db.query_all_routers(conn))
    else:
        console.print("SNOW Tickets ([green]device serial[/], [green]ticket id[/]):")
        console.print(db.query_all_tickets(conn))

    # close the database connection
    db.close_connection(conn)


def main():
    # Process option args
    parser = argparse.ArgumentParser(description="Populates Database with topology information (Cameras, Switches, "
                                                 "Routers)", epilog="Run with no arguments to build full topology "
                                                                    "table (warning: this can be time intensive!)")
    parser.add_argument("--print", "-p", help="Print Table Contents", choices=['camera', 'switch', 'router', 'ticket'],
                        required=False)
    args = parser.parse_args()

    if args.print:
        console.print(Panel.fit(f"Printing {args.print.capitalize()} Table Contents:"))

        # If print option provided, display table contents
        print_table(args.print)
    else:
        console.print(Panel.fit("Building Full Topology Table:"))

        # Run full topology building workflow
        build_full_topology()


if __name__ == "__main__":
    main()
