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
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

import meraki
import requests
from celery import Celery, chain
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
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
dashboard = meraki.DashboardAPI(api_key=config.MERAKI_API_KEY, suppress_logging=True, maximum_retries=5)

# Global Variables
DELAY_TIME = 5  # time to wait (minute) between polls (a value of 0 skips sleeping)

# Rich Console Instance
console = Console()

# Custom logger section
FORMATTER = logging.Formatter('%(asctime)s [%(levelname)s]: %(message)s')

# Scheduler Section
scheduler = BackgroundScheduler()
scheduler.start()

TICKET_REMOVAL_TIME = 1  # time to wait (hours) to check if a device is active and a ticket is stale


def custom_logger(serial):
    """
    Define custom logger for each celery task, writes logs to serial stamped file. Creates or returns existing logger
    :param serial: Webhook device serial
    :return: logger instance
    """
    logger = logging.getLogger(serial)

    if not logger.handlers:
        logFile = os.path.join('logs/', serial + '.log')

        # Current log policies: 5 MB max size
        my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5 * 1024 * 1024)
        my_handler.setFormatter(FORMATTER)
        my_handler.setLevel(logging.INFO)

        logger.addHandler(my_handler)

        logger.setLevel(logging.INFO)

    return logger


def run_stamp(serial):
    """
    Initial meta data for each run, appended to device log file
    :param serial: Webhook device serial
    :return:
    """
    with open(f'logs/{serial}.log', 'a') as fp:
        fp.write(f'**************************** New Run: {serial} **************************************************\n')
        fp.write(f'**************************** Time Stamp: {datetime.datetime.now()} ****************************\n')


def find_switchport(switch_serial, camera_mac):
    """
    Find switch-port on connected switch that the camera is connected to, supports same or cross network
    :param switch_serial: Switch Serial, used for checking port CDP/LLDP information
    :param camera_mac: Camera MAC address (mapped to camera on port)
    :return: Port number of camera switch port, API error encountered (if relevant)
    """
    # Get Network topology (link layer) information
    try:
        ports = dashboard.switch.getDeviceSwitchPortsStatuses(switch_serial)
    except Exception as e:
        # All exceptions (invalid network, etc.) are bubbled up
        return None, str(e)

    # Iterate through ports, if the port is enabled and has cdp or lldp information (match the mac - this determines
    # port)
    for port in ports:
        # Check lldp
        if 'lldp' in port and 'chassisId' in port['lldp']:
            if port['lldp']['chassisId'] == camera_mac:
                # we found it!
                return port['portId'], None
        # Check cdp
        elif 'cdp' in port and 'deviceId' in port['cdp']:
            # need to translate mac to appropriate format (api returns mac with now ':')
            converted_camera_mac = camera_mac.replace(":", "")
            if port['cdp']['deviceId'] == converted_camera_mac:
                # we found it!
                return port['portId'], None

    return None, None


def find_switchport_status(switch_serial, switch_port):
    """
    Return switchport status (warnings and errors) after debug routine, help illuminate underlying errors
    :param switch_serial: MS Serial
    :param switch_port: MS switchport MV is connected to
    :return: Errors, Warnings
    """
    # Collect any error and warning data from switchport (over the time we've been waiting)
    port_statuses = dashboard.switch.getDeviceSwitchPortsStatuses(switch_serial)

    # Identify statuses of correct port
    for port_status in port_statuses:
        if 'portId' in port_status and port_status['portId'] == switch_port:
            target_port = port_status

            # Return any states errors or warnings on port
            return target_port['errors'], target_port['warnings']

    return None, None


@celery.task
def debug_mv_camera(org_id, serial, camera_name, switch_serial):
    """
    Trigger primary debugging workflow for MV camera, the steps are outlined in the README
    :param org_id: Org ID
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
    camera_mac = response[0]['mac']
    # If camera is online, stop further processing
    if status == 'online':
        l.info(f'- Camera is back online!')
        return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
                "switch_port": None, "api_error": None, "switch_port_status": {"errors": None, "warnings": None}}

    l.error(f'- Camera is still offline...')

    # Get topology information, determine the switch the camera was connected too
    l.info(f'Finding connected switch serial and port from topology...')
    switch_port, api_error = find_switchport(switch_serial, camera_mac)

    # If no port found, return
    if not switch_port:
        if api_error:
            l.error(f'- Unable to find connected switch port, API error: {api_error}')
        else:
            l.error(f'- Unable to find connected switch port, no port found')
        return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
                "switch_port": None, "api_error": api_error, "switch_port_status": {"errors": None, "warnings": None}}

    l.info(
        f'- Found Camera is connected to switch serial: {switch_serial} on port: {switch_port}')

    # Cycle port
    l.info(f'Cycling Switch Port...')
    try:
        response = dashboard.switch.cycleDeviceSwitchPorts(switch_serial, ports=[switch_port])
    except Exception as e:
        l.error(f'- Unable to cycle switch port, API error: {str(e)}, generating ticket...')
        return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
                "switch_port": switch_port, "api_error": str(e),
                "switch_port_status": {"errors": None, "warnings": None}}

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

    # Collect any error and warning data from switchport (over the time we've been waiting)
    errors, warnings = find_switchport_status(switch_serial, switch_port)

    return {"serials": serial, "status": status, "names": camera_name, "switch_serial": switch_serial,
            "switch_port": switch_port, "api_error": None,
            "switch_port_status": {"errors": errors, "warnings": warnings}}


def find_impacted_cameras(serial, starting_point, conn):
    """
    Find cameras impacted by MX or MS outage based on DB Topology, include these in the Ticket output
    :param conn: DB Connection Object
    :param serial: Device Serial (MX or MS)
    :param starting_point: What level to search for impacted devices (router or switch level)
    :return: list of impact camera serials
    """
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

    return impacted_cameras


def generate_ticket_data(processing_data, webhook_data, conn):
    """
    Generate Ticket Data (local CSV logging, ServiceNow Tickets)
    :param conn: DB Connection Object
    :param processing_data: Data returned from debugging workflow or including
    :param webhook_data: Webhook information returned from Meraki
    :return: ticket data
    """
    # Webhook Event time stamp
    d = datetime.datetime.fromisoformat(webhook_data['occurredAt'][:-1]).astimezone(datetime.timezone.utc)
    dt_string = d.strftime('%Y-%m-%d %H:%M:%S')

    ticket_data = {'Timestamp': dt_string, 'Alert Type': webhook_data["alertType"],
                   'Network': webhook_data['networkName']}

    # Cases:
    # Debug routine (only process cameras still offline), Critical Hardware Failure
    if (ticket_data['Alert Type'] == "cameras went down" and processing_data['status'] == 'offline') or ticket_data[
        'Alert Type'] == "Camera may have critical hardware failure":
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

        # If an API error happened during processing, bubble up to the ticket
        if processing_data['api_error']:
            ticket_data['API Error'] = processing_data['api_error']

        # If there's switchport status information (both warnings and errors are not None)
        if processing_data['switch_port_status']['errors'] is not None and processing_data['switch_port_status'][
            'warnings'] is not None:
            ticket_data['Switch Port Errors'] = processing_data['switch_port_status']['errors']
            ticket_data['Switch Port Warnings'] = processing_data['switch_port_status']['warnings']

    # MX Went down
    elif ticket_data['Alert Type'] == "appliances went down":
        # Build ticket contents
        ticket_data['Affected Device Type'] = 'Router'
        ticket_data['Affected Device Name'] = webhook_data['deviceName']
        ticket_data['Affected Device Serial'] = webhook_data['deviceSerial']

        # Impacted Cameras (calculate all impacted cameras downstream)
        cameras = find_impacted_cameras(webhook_data['deviceSerial'], 'router', conn)

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
        cameras = find_impacted_cameras(webhook_data['deviceSerial'], 'switch', conn)

        ticket_data['Impacted Camera Name(s)'] = [cam[1] for cam in cameras]
        ticket_data['Impacted Camera Serial(s)'] = [cam[0] for cam in cameras]

        # Upstream switch connection
        ticket_data['Upstream Switch Serial'] = 'N/A'
        ticket_data['Upstream Switch Name'] = 'N/A'
        ticket_data['Upstream Switch Port'] = 'N/A'
    else:
        return

    return ticket_data


def log_ticket_information(ticket_data, webhook_data):
    """
    Log relevant ServiceNow ticket information to CSV file
    :param ticket_data: Data returned from debugging workflow, processed
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
                      'Upstream Switch Serial', 'Upstream Switch Name', 'Upstream Switch Port', 'Switch Port Warnings', 'Switch Port Errors']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        # Add header if file doesn't exist
        if not file_exists:
            writer.writeheader()

        # Write to csv
        writer.writerow(ticket_data)


def service_now_ticket_cleanup(org_id, serial, snow_sys_id):
    """
    Check if device is currently online (this means the ticket is old, and should be removed, or there are newer
    tickets if the device 'flapped')
    :param org_id: Meraki Org ID
    :param serial: Meraki Device Serial (Associated with SNOW Ticket)
    :param snow_sys_id: SNOW Unique Ticket ID
    :return:
    """
    response = dashboard.organizations.getOrganizationDevicesStatuses(org_id, serials=[serial])
    status = response[0]['status']

    l = custom_logger('snow-cleanup')

    # If camera is offline, stop cleanup of tickets
    if status == 'online':
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = (config.SERVICENOW_USERNAME, config.SERVICENOW_PASSWORD)

        # Check if ticket exits and isn't resolved
        response = requests.get(config.SERVICENOW_INSTANCE + f"/api/now/table/incident", auth=auth,
                                params={'sys_id': snow_sys_id},
                                headers=headers)

        l.info(f'Found existing ticket {snow_sys_id} while device is online, currently not resolved')

        if response.ok:
            result = response.json()['result']
            if len(result) > 0 and result[0]['state'] != 7:
                # Result found (not deleted), state not equal to 7 (not resolved)

                # Get ServiceNow caller
                servicenow_caller = requests.get(
                    config.SERVICENOW_INSTANCE + "/api/now/table/sys_user?sysparm_query=user_name%3D" + config.SERVICENOW_USERNAME,
                    auth=auth, headers=headers).json()['result'][0]['name']

                # Set SNOW Ticket to resolved, add automated comment
                updated_ticket = {
                    "caller_id": servicenow_caller,
                    "state": "6",
                    "comments": "This Ticket has been Automatically Marked Resolved, the underlying device has been "
                                f"online for {TICKET_REMOVAL_TIME} + Hours. "
                }
                response = requests.put(config.SERVICENOW_INSTANCE + f"/api/now/table/incident/{snow_sys_id}",
                                        auth=auth,
                                        headers=headers, json=updated_ticket)

                if response.ok:
                    l.info(f'Successfully set ticket {snow_sys_id} to "resolved" state')
                else:
                    l.error(f'{response.text}')
            else:
                l.error(f'Ticket not found or already Resolved! Skipping')
        else:
            l.error(f'{response.text}')


@celery.task
def create_service_now_ticket(processing_data, webhook_data):
    """
    Create ServiceNow Ticket, include webhook and troubleshooting result data, log ticket data to CSV
    :param processing_data: Troubleshooting results (if applicable)
    :param webhook_data: Webhook data
    :return:
    """
    conn = db.create_connection("sqlite.db")

    # Generate Detailed Ticket Data
    ticket_data = generate_ticket_data(processing_data, webhook_data, conn)

    # If service now ticket functionality enabled
    if config.SERVICE_NOW_ENABLED:
        l = custom_logger(webhook_data['deviceSerial'])
        l.info(f'Creating Service Now Ticket for: {webhook_data}')

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = (config.SERVICENOW_USERNAME, config.SERVICENOW_PASSWORD)

        # Get ServiceNow caller
        servicenow_caller = requests.get(
            config.SERVICENOW_INSTANCE + "/api/now/table/sys_user?sysparm_query=user_name%3D" + config.SERVICENOW_USERNAME,
            auth=auth, headers=headers).json()['result'][0]['name']

        # Set Impact and Urgency based on type of alert
        if ticket_data['Alert Type'] == "cameras went down":
            impact = "2"
            urgency = "3"
        elif ticket_data['Alert Type'] == "switches went down":
            impact = "2"
            urgency = "2"
        elif ticket_data['Alert Type'] == "appliances went down":
            impact = "2"
            urgency = "1"
        elif ticket_data["Alert Type"] == "Camera may have critical hardware failure":
            impact = "2"
            urgency = "1"
        else:
            impact = "3"
            urgency = "3"

        # Build ticket information
        ticket = {
            "caller_id": servicenow_caller,
            "impact": impact,
            "urgency": urgency,
            "category": "Network",
            "short_description": ticket_data['Alert Type'] + " (Alert ID: " + webhook_data['alertId'] + ")",
            "description": "The full Meraki ticket is:  \n" + json.dumps(ticket_data, indent=4)
        }

        # Create new ServiceNow Ticket
        response = requests.post(config.SERVICENOW_INSTANCE + "/api/now/table/incident", auth=auth, headers=headers,
                                 json=ticket)

        if response.ok:
            ticket_details = response.json()
            l.info(f'A New ticket was created with Incident Number: {ticket_details["result"]["number"]}')

            # Keep Track of Tickets in DB table
            db.add_snow_ticket(conn, webhook_data['deviceSerial'], ticket_details["result"]["sys_id"])
        else:
            l.error(f'Failed to create Service Now Ticket: {response.text}')

    # Log ticket data to CSV as well
    log_ticket_information(ticket_data, webhook_data)

    db.close_connection(conn)


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

        # Shared Secret Check
        if data['sharedSecret'] != config.SHARED_SECRET:
            console.print("[red]Error, shared secret doesn't match configured shared secret... ignoring[/]")

        # Optional: filter for specific network
        if len(config.TARGET_NETWORKS) > 0 and not data['networkName'] in config.TARGET_NETWORKS:
            console.print("[red]Webhook ignored, network name not present in target networks list....ignoring[/]")
            return 'Webhook ignored, network not present in whitelist - check the terminal for more information'

        # The database holds information about the status of the Meraki devices and the topology of the network
        conn = db.create_connection("sqlite.db")

        if data["alertType"] == "cameras went down":
            # Extract variables from webhook
            org_id = data['organizationId']
            serial = data['deviceSerial']

            # check what the camera status is
            camera_status = db.query_camera_status(conn, serial)
            console.print(f"Camera ({serial}) topology status is: {camera_status}")

            if camera_status and camera_status[0][0] == "up":
                # The Camera is down, so we need to update the database to reflect this
                db.update_device_status(conn, "camera", serial, "down")

                # Now we need to check to see if the Camera connection is also down before we create a ticket
                connection = db.query_camera_connection(conn, serial)

                switch_serial = connection[0][0]
                switch_status = db.query_switch_status(conn, switch_serial)
                console.print(f"Connected Switch ({switch_serial}) current status is: {switch_status}")

                # If the switch status is up, we create a ticket
                if switch_status[0][0] == "up":
                    # Check if a ticket already exists for the Camera (from critical hardware failure, or another
                    # ticket)
                    ticket = db.query_specific_snow_ticket(conn, serial)

                    if config.DUPLICATE_TICKETS and len(ticket) > 0:
                        console.print(
                            f"No ticket created for the Camera, existing SNOW ticket present: {ticket[0][0]}")
                    else:
                        # Pass processing off to celery worker
                        console.print(f'Passing processing to [green]celery worker[/]...')

                        # Build chain of celery tasks, once main debug loop complete, write results to csv file,
                        # create SNOW ticket
                        chain(debug_mv_camera.s(org_id, serial, data['deviceName'], switch_serial),
                              create_service_now_ticket.s(data)).apply_async()

                else:
                    console.print("No ticket created for the Camera")

        elif data["alertType"] == "Camera may have critical hardware failure":
            # Camera has critical hardware failure
            org_id = data['organizationId']
            serial = data["deviceSerial"]

            # Check if camera is in DB
            camera_status = db.query_camera_status(conn, serial)

            if camera_status:
                # Now we need to check to see if the Camera connection is also down before we create a ticket
                connection = db.query_camera_connection(conn, serial)
                switch_serial = connection[0][0]

                # Check if a ticket already exists for the Camera (from critical hardware failure, or another
                # ticket)
                ticket = db.query_specific_snow_ticket(conn, serial)

                if config.DUPLICATE_TICKETS and len(ticket) > 0:
                    console.print(
                        f"No ticket created for the Camera, existing SNOW ticket present: {ticket[0][0]}")
                else:
                    # Pass processing off to celery worker
                    console.print(f'Passing processing to [green]celery worker[/]...')

                    # Build chain of celery tasks, once main debug loop complete, write results to csv file,
                    # create SNOW ticket
                    chain(debug_mv_camera.s(org_id, serial, data['deviceName'], switch_serial),
                          create_service_now_ticket.s(data)).apply_async()

        elif data["alertType"] == "switches went down":
            serial = data["deviceSerial"]
            # check what the switch status is
            switch_status = db.query_switch_status(conn, serial)
            if switch_status and switch_status[0][0] == "up":
                # The switch is now down, so we need to update the database to reflect this
                db.update_device_status(conn, "switch", serial, "down")

                # Now we need to check to see if the switch connection is also down before we create a ticket
                connection = db.query_switch_connection(conn, serial)
                if connection[0][0] is not None:
                    router_status = db.query_router_status(conn, connection[0][0])
                    # If the router status is up, we create a ticket
                    if router_status[0][0] == "up":
                        # Log ticket information, create ServiceNow ticket
                        create_service_now_ticket.delay(None, data)
                        console.print("Ticket created for the switch")
                    else:
                        console.print("No ticket needed for the switch")
                # there is no connection to the switch, we should create a ticket
                else:
                    # Log ticket information, create ServiceNow ticket
                    create_service_now_ticket.delay(None, data)
                    console.print("Ticket created for the switch")
            else:
                # switch is already down, ticket should have already been created
                console.print("No ticket created, switch is already down or not in DB. Check for existing ticket.")

        elif data["alertType"] == "appliances went down":
            serial = data["deviceSerial"]
            # check what the router status is
            router_status = db.query_router_status(conn, serial)
            if router_status and router_status[0][0] == "up":
                # The router is now down, so we need to update the database to reflect this
                db.update_device_status(conn, "router", serial, "down")

                # Log ticket information, create ServiceNow ticket
                create_service_now_ticket.delay(None, data)
                console.print("Ticket created for the router")
            else:
                # router is already down, ticket should have already been created
                console.print("No ticket created, router is already down or not in DB. Check for existing ticket")
        elif data["alertType"] == "switches came up":
            # Grab Data from Webhook
            serial = data["deviceSerial"]
            org_id = data['organizationId']

            # The switch is up, so we need to update the database to reflect this
            db.update_device_status(conn, "switch", serial, "up")
            console.print(f"Switch ({serial}) is back up")

            # query ticket from DB
            data = db.query_specific_snow_ticket(conn, serial)

            if len(data) > 0:
                # Ticket present, delete from DB
                db.delete_snow_ticket(conn, serial)

                if config.TICKET_CLEANUP:
                    # Remove SNOW ticket after set amount of hours if device is online at that time
                    scheduler.add_job(
                        service_now_ticket_cleanup, args=[org_id, serial, data[0][0]], trigger='date',
                        run_date=datetime.datetime.now() + datetime.timedelta(hours=TICKET_REMOVAL_TIME)
                    )

        elif data["alertType"] == "appliances came up":
            # Grab Data from Webhook
            serial = data["deviceSerial"]
            org_id = data['organizationId']

            # The router is up, so we need to update the database to reflect this
            db.update_device_status(conn, "router", serial, "up")
            console.print(f"Router ({serial}) is back up")

            # query ticket from DB
            data = db.query_specific_snow_ticket(conn, serial)

            if len(data) > 0:
                # Ticket present, delete from DB
                db.delete_snow_ticket(conn, serial)

                if config.TICKET_CLEANUP:
                    # Remove SNOW ticket after set amount of hours if device is online at that time
                    scheduler.add_job(
                        service_now_ticket_cleanup, args=[org_id, serial, data[0][0]], trigger='date',
                        run_date=datetime.datetime.now() + datetime.timedelta(hours=TICKET_REMOVAL_TIME)
                    )

        elif data["alertType"] == "cameras came up":
            # Grab Data from Webhook
            serial = data["deviceSerial"]
            org_id = data['organizationId']

            # The Camera is up, so we need to update the database to reflect this
            db.update_device_status(conn, "camera", serial, "up")
            console.print(f"Camera ({serial}) is back up")

            # query ticket from DB
            data = db.query_specific_snow_ticket(conn, serial)

            if len(data) > 0:
                # Ticket present, delete from DB
                db.delete_snow_ticket(conn, serial)

                if config.TICKET_CLEANUP:
                    # Remove SNOW ticket after set amount of hours if device is online at that time
                    scheduler.add_job(
                        service_now_ticket_cleanup, args=[org_id, serial, data[0][0]], trigger='date',
                        run_date=datetime.datetime.now() + datetime.timedelta(hours=TICKET_REMOVAL_TIME)
                    )

        db.close_connection(conn)

    return 'Webhook receiver is running - check the terminal for alert information'


if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
