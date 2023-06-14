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

import csv
import datetime
import logging
import os
import time
from datetime import datetime
from datetime import timezone
from logging.handlers import RotatingFileHandler

import meraki
from celery import Celery, chain
from flask import Flask, request
from rich.console import Console
from rich.panel import Panel

import config
import db

# Global Flask flask_app
app = Flask(__name__)

celery = Celery(app.name)
celery.conf.broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379")
celery.conf.result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379")
celery.conf.worker_hijack_root_logger = False

# Meraki Dashboard Instance
dashboard = meraki.DashboardAPI(api_key=config.MERAKI_API_KEY, suppress_logging=True)

# Global Variables
DELAY_TIME = 5  # time to wait between polls in minutes

# Rich Console Instance
console = Console()

# Custom logger section
FORMATTER = logging.Formatter('%(asctime)s [%(levelname)s]: %(message)s')


def custom_logger(serial):
    """
    Define custom logger for each celery task, writes logs to serial stamped file. Creates or returns existing logger
    :param serial: Webhook device serial
    :return: logger instance
    """
    logger = logging.getLogger(serial)

    if not logger.handlers:
        logFile = os.path.join('./logs/', serial + '.log')

        # Current log policies: 5 MB max size
        my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5 * 1024 * 1024)
        my_handler.setFormatter(FORMATTER)
        my_handler.setLevel(logging.INFO)

        logger.addHandler(my_handler)

    return logger


def run_stamp(serial):
    """
    Initial meta data for each run, appended to device log file
    :param serial: Webhook device serial
    :return:
    """
    with open(f'logs/{serial}.log', 'a') as fp:
        fp.write(f'**************************** New Run: {serial} **************************************************\n')
        fp.write(f'**************************** Time Stamp: {datetime.now()} ****************************\n')


def find_switchport(network_id, serial):
    """
    Find switch-port on connected switch that the camera is connected to, supports same or cross network
    :param network_id: Webhook network id
    :param serial: Camera serial, used to identify correct link in topology
    :return: Port number of camera switch port
    """
    # Get Network topology (link layer) information
    try:
        response = dashboard.networks.getNetworkTopologyLinkLayer(network_id)
    except Exception:
        # All exceptions (invalid network, etc. are skipped)
        return None

    # Check all reported links and ends to find the right link (camera -> switch)
    for link in response['links']:
        for end in link['ends']:
            if 'device' in end and end['device']['serial'] == serial:
                # Found the right link, return switch serial (by lldp or cdp depending on what's not null)
                if end['discovered']['lldp']:
                    return end['discovered']['lldp']['portId'].strip('Port ')
                elif end['discovered']['cdp']:
                    return end['discovered']['cdp']['portId'].strip('Port ')
                else:
                    return None

    return None


@celery.task
def debug_mv_camera(org_id, network_id, serial, camera_name, switch_serial):
    """
    Trigger primary debugging workflow for MV camera, the steps are outlined in the README
    :param org_id: Org ID
    :param network_id: Camera network ID
    :param serial: Camera serial
    :param camera_name: Camera name
    :param switch_serial: Connected switch serial
    :return: Information gathered from debugging (switch-port, switch name, etc.)
    """
    # Create log file, create customer logger instance
    run_stamp(serial)
    l = custom_logger(serial)

    # Camera is down, wait for DELAY_TIME and re-query to confirm it's down
    l.info(f'Sleeping for {DELAY_TIME} minutes...')
    time.sleep(60 * DELAY_TIME)

    # Query Camera status to see if it's still offline
    l.info(f'Checking Camera serial {serial} status...')
    response = dashboard.organizations.getOrganizationDevicesStatuses(org_id, serials=[serial])

    status = response[0]['status']
    # If camera is online, stop further processing
    if status == 'online':
        l.info(f'- Camera is back online!')
        return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
                "switch_port": None}

    l.error(f'- Camera is still offline...')

    # Get topology information, determine the switch the camera was connected too
    l.info(f'Finding connected switch serial and port from topology...')
    switch_port = find_switchport(network_id, serial)

    # If no port found, return
    if not switch_port:
        return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
                "switch_port": None}

    l.info(
        f'- Found Camera is connected to switch serial: {switch_serial} on port: {switch_port}')

    # Cycle port
    l.info(f'Cycling Switch Port...')
    try:
        response = dashboard.switch.cycleDeviceSwitchPorts(switch_serial, ports=[switch_port])
    except Exception as e:
        l.error(f'- Unable to cycle switchport: {str(e)}, generating ticket...')
        return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
                "switch_port": switch_port}

    l.info(f'- Successfully cycled ports: {response["ports"]}')

    # Port cycled, wait before checking again
    l.info(f'Sleeping for {DELAY_TIME} minutes...')
    time.sleep(60 * DELAY_TIME)

    # Query Camera status to see if it's still offline
    response = dashboard.organizations.getOrganizationDevicesStatuses(org_id, serials=[serial])
    status = response[0]['status']

    # If camera is online, stop further processing
    if status == 'online':
        l.info(f'- Camera is back [green]online[/]!')
    else:
        l.error(f'- Camera is still offline... Generating ServiceNow Ticket and logging...')

    return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
            "switch_port": switch_port}


def find_impacted_cameras(serial, starting_point):
    """
    Find cameras impacted by MX or MS outage based on DB Topology, include these in the Ticket output
    :param serial: Device Serial (MX or MS)
    :param starting_point: What level to search for impacted devices (router or switch level)
    :return: list of impact camera serials
    """
    # DB Connection
    conn = db.create_connection("sqlite.db")
    impacted_cameras = []

    if starting_point == 'router':
        # Find all connected switches to router
        switches = db.query_connected_switches_to_router(conn, serial)

        # Find all connected cameras to each switch
        for switch in switches:
            cameras = db.query_connected_cameras_to_switches(conn, switch[0])

            # Add all downstream cameras to list
            for camera in cameras:
                device = dashboard.devices.getDevice(camera[0])
                device_name = device['name']

                impacted_cameras.append((camera[0], device_name))

    elif starting_point == 'switch':
        cameras = db.query_connected_cameras_to_switches(conn, serial)

        # Add all downstream cameras to list
        for camera in cameras:
            device = dashboard.devices.getDevice(camera[0])
            device_name = device['name']

            impacted_cameras.append((camera[0], device_name))

    # Close DB connection
    db.close_connection(conn)

    return impacted_cameras


@celery.task
def log_ticket_information(processing_data, webhook_data):
    """
    Log relevant ServiceNow ticket information to CSV file
    :param processing_data: Data returned from debugging workflow or including
    :param webhook_data: Webhook information returned from Meraki
    :return:
    """
    l = custom_logger(webhook_data['deviceSerial'])
    l.info(f'Logging {webhook_data} to CSV file')

    file_exists = os.path.isfile(config.TICKET_CSV_PATH)

    # Append ticket information to csv file
    with open(config.TICKET_CSV_PATH, 'a') as csvfile:
        fieldnames = ['Timestamp', 'Alert Type', 'Network', 'Affected Device Type', 'Affected Device Name',
                      'Affected Device Serial', 'Impacted Camera Name(s)', 'Impacted Camera Serial(s)',
                      'Upstream Switch Serial', 'Upstream Switch Name', 'Upstream Switch Port']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        # Add header if file doesn't exist
        if not file_exists:
            writer.writeheader()

        # Webhook Event time stamp
        d = datetime.fromisoformat(webhook_data['occurredAt'][:-1]).astimezone(timezone.utc)
        dt_string = d.strftime('%Y-%m-%d %H:%M:%S')

        ticket_data = {'Timestamp': dt_string, 'Alert Type': webhook_data["alertType"],
                       'Network': webhook_data['networkName']}

        # Cases:
        # Debug routine (only process cameras still offline)
        if ticket_data['Alert Type'] == "cameras went down" and processing_data['status'] == 'offline':
            # Build ticket contents
            ticket_data['Affected Device Type'] = 'Camera'
            ticket_data['Affected Device Name'] = webhook_data['deviceName']
            ticket_data['Affected Device Serial'] = webhook_data['deviceSerial']

            # Impacted Cameras
            ticket_data['Impacted Camera Name(s)'] = 'N/A'
            ticket_data['Impacted Camera Serial(s)'] = 'N/A'

            # Upstream switch connection
            if processing_data['switch_serial']:
                ticket_data['Upstream Switch Serial'] = processing_data['switch_serial']

                device = dashboard.devices.getDevice(processing_data['switch_serial'])
                ticket_data['Upstream Switch Name'] = device['name']
            else:
                ticket_data['Upstream Switch Serial'] = 'N/A'
                ticket_data['Upstream Switch Name'] = 'N/A'

            # If switch port is not none
            if processing_data['switch_port']:
                ticket_data['Upstream Switch Port'] = processing_data['switch_port']
            else:
                ticket_data['Upstream Switch Port'] = 'Not found'
        # MX Went down
        elif ticket_data['Alert Type'] == "appliances went down":
            # Build ticket contents
            ticket_data['Affected Device Type'] = 'Router'
            ticket_data['Affected Device Name'] = webhook_data['deviceName']
            ticket_data['Affected Device Serial'] = webhook_data['deviceSerial']

            # Impacted Cameras (calculate all impacted cameras downstream)
            cameras = find_impacted_cameras(webhook_data['deviceSerial'], 'router')

            ticket_data['Impacted Camera Name(s)'] = [cam[1] for cam in cameras]
            ticket_data['Impacted Camera Serial(s)'] = [cam[0] for cam in cameras]

            # Upstream switch connection
            ticket_data['Upstream Switch Serial'] = 'N/A'
            ticket_data['Upstream Switch Name'] = 'N/A'
            ticket_data['Upstream Switch Port'] = 'N/A'
        elif ticket_data['Alert Type'] == "switches went down":
            # Build ticket contents
            ticket_data['Affected Device Type'] = 'Switch'
            ticket_data['Affected Device Name'] = webhook_data['deviceName']
            ticket_data['Affected Device Serial'] = webhook_data['deviceSerial']

            # Impacted Cameras (calculate all impacted cameras downstream)
            cameras = find_impacted_cameras(webhook_data['deviceSerial'], 'switch')

            ticket_data['Impacted Camera Name(s)'] = [cam[1] for cam in cameras]
            ticket_data['Impacted Camera Serial(s)'] = [cam[0] for cam in cameras]

            # Upstream switch connection
            ticket_data['Upstream Switch Serial'] = 'N/A'
            ticket_data['Upstream Switch Name'] = 'N/A'
            ticket_data['Upstream Switch Port'] = 'N/A'
        else:
            return

        # Write to csv
        writer.writerow(ticket_data)


@app.route("/alerts", methods=["GET", "POST"])
def meraki_alert():
    """
    The webhooks will send information to this web server, and this function
    provides the logic to parse the Meraki alert
    """
    # If the method is POST, then an alert has sent a webhook to the web server
    if request.method == "POST":
        console.print(Panel.fit("Webhook Alert Detected:"))
        data = request.json  # Retrieve the json data from the request - contains alert info
        console.print(data)

        # The database holds information about the status of the Meraki devices and the topology of the network
        conn = db.create_connection("sqlite.db")

        if data["alertType"] == "cameras went down":
            # Extract variables from webhook
            org_id = data['organizationId']
            network_id = data['networkId']
            serial = data['deviceSerial']

            # check what the camera status is
            camera_status = db.query_camera_status(conn, serial)
            console.print(f"Camera ({serial}) topology status is: {camera_status}")

            if camera_status[0][0] == "up":
                # The Camera is down, so we need to update the database to reflect this
                db.update_device_status(conn, "camera", serial, "down")

                # Now we need to check to see if the Camera connection is also down before we create a ticket
                connection = db.query_camera_connection(conn, serial)

                switch_serial = connection[0][0]
                switch_status = db.query_switch_status(conn, switch_serial)
                console.print(f"Connected Switch ({switch_serial}) current status is: {switch_status}")

                # If the switch status is up, we create a ticket
                if switch_status[0][0] is not None:
                    if switch_status[0][0] == "up":
                        # Pass processing off to celery worker
                        console.print(f'Passing processing to [green]celery worker[/]...')

                        # Build chain of celery tasks, once main debug loop complete, write results to csv file
                        chain(debug_mv_camera.s(org_id, network_id, serial, data['deviceName'], switch_serial),
                              log_ticket_information.s(data)).apply_async()
                    else:
                        console.print("No ticket created for the Camera")
                else:
                    # camera is not connected to device in database, create ticket
                    ticket_data = {"serials": None, "status": 'offline', "names": None, "switch_serial": None,
                                   "switch_port": None}

                    log_ticket_information(ticket_data, data)
                    console.print("Ticket created for the camera")

        elif data["alertType"] == "switches went down":
            serial = data["deviceSerial"]
            # check what the switch status is
            switch_status = db.query_switch_status(conn, serial)
            if switch_status[0][0] == "up":
                # The switch is now down, so we need to update the database to reflect this
                db.update_device_status(conn, "switch", serial, "down")

                # Now we need to check to see if the switch connection is also down before we create a ticket
                connection = db.query_switch_connection(conn, serial)
                if connection[0][0] is not None:
                    router_status = db.query_router_status(conn, connection[0][0])
                    # If the router status is up, we create a ticket
                    if router_status[0][0] == "up":
                        # Add ticket information to CSV File
                        log_ticket_information.delay(None, data)

                        console.print("Ticket created for the switch")
                    else:
                        console.print("No ticket needed for the switch")
                # there is no connection to the switch, we should create a ticket
                else:
                    # Add ticket information to CSV File
                    log_ticket_information.delay(None, data)

                    console.print("Ticket created for the switch")
            else:
                # switch is already down, ticket should have already been created
                console.print("No ticket created, switch is already down. Check for existing ticket")

        elif data["alertType"] == "appliances went down":
            serial = data["deviceSerial"]
            # check what the router status is
            router_status = db.query_router_status(conn, serial)
            if router_status[0][0] == "up":
                # The router is now down, so we need to update the database to reflect this
                db.update_device_status(conn, "router", serial, "down")

                # Add ticket information to CSV File
                log_ticket_information.delay(None, data)

                console.print("Ticket created for the router")
            else:
                # router is already down, ticket should have already been created
                console.print("No ticket created, router is already down. Check for existing ticket")
        elif data["alertType"] == "switches came up":
            serial = data["deviceSerial"]
            # The switch is up, so we need to update the database to reflect this
            db.update_device_status(conn, "switch", serial, "up")
            console.print(f"Switch ({serial}) is back up")
        elif data["alertType"] == "appliances came up":
            serial = data["deviceSerial"]
            # The router is up, so we need to update the database to reflect this
            db.update_device_status(conn, "router", serial, "up")
            console.print(f"Router ({serial}) is back up")
        elif data["alertType"] == "cameras came up":
            serial = data["deviceSerial"]
            # The Camera is up, so we need to update the database to reflect this
            db.update_device_status(conn, "camera", serial, "up")
            console.print(f"Camera ({serial}) is back up")

        db.close_connection(conn)

    return 'Webhook receiver is running - check the terminal for alert information'


if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
