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

import sqlite3
from pprint import pprint
from sqlite3 import Error


def create_connection(db_file: str) -> sqlite3.Connection | None:
    """
    Connect to DB
    :param db_file: DB Object
    :return: Connections Object
    """
    conn = None
    try:
        conn = sqlite3.connect(db_file)

        return conn
    except Error as e:
        print(e)

        return None


def create_tables(conn: sqlite3.Connection):
    """
    Create Empty Tables (Routers, Switches, Cameras, SNOW Tickets)
    :param conn: DB Connection Object
    """
    c = conn.cursor()

    # Delete table if it exists already
    c.execute("DROP TABLE IF EXISTS routers")

    c.execute("""
              CREATE TABLE IF NOT EXISTS routers
              ([serial] TEXT PRIMARY KEY,
               [status] TEXT)
              """)

    # Delete table if it exists already
    c.execute("DROP TABLE IF EXISTS switches")

    c.execute("""
              CREATE TABLE IF NOT EXISTS switches
              ([serial] TEXT PRIMARY KEY,
               [connection] TEXT,
               [status] TEXT,
              FOREIGN KEY (connection) REFERENCES routers (serial))
              """)

    # Delete table if it exists already
    c.execute("DROP TABLE IF EXISTS cameras")

    c.execute("""
              CREATE TABLE IF NOT EXISTS cameras
              ([serial] TEXT PRIMARY KEY,
               [connection] TEXT,
               [status] TEXT,
              FOREIGN KEY (connection) REFERENCES switches (serial))
              """)

    # Delete table if it exists already
    c.execute("DROP TABLE IF EXISTS snow_tickets")

    c.execute("""
              CREATE TABLE IF NOT EXISTS snow_tickets
              ([serial] TEXT PRIMARY KEY,
               [incident_sys_id] TEXT)
              """)

    conn.commit()


def query_all_routers(conn: sqlite3.Connection) -> list:
    """
    Return all routers in Router Table
    :param conn: DB Connection Object
    :return: List of routers
    """
    c = conn.cursor()

    c.execute("""
              SELECT *
              FROM routers
              """)
    routers = c.fetchall()

    return routers


def query_all_switches(conn: sqlite3.Connection) -> list:
    """
    Return all switches in Switches Table
    :param conn: DB Connection Object
    :return: List of switches
    """
    c = conn.cursor()

    c.execute("""
              SELECT *
              FROM switches
              """)
    switches = c.fetchall()

    return switches


def query_all_cameras(conn: sqlite3.Connection) -> list:
    """
    Return all cameras in Camera Table
    :param conn: DB Connection Object
    :return: List of cameras
    """
    c = conn.cursor()

    c.execute("""SELECT *
              FROM cameras
              """)
    cameras = c.fetchall()

    return cameras


def query_all_tickets(conn: sqlite3.Connection) -> list:
    """
    Return all snow_tickets in Tickets Table
    :param conn: DB Connection Object
    :return: List of SNOW Tickets
    """
    c = conn.cursor()

    c.execute("""SELECT *
              FROM snow_tickets
              """)
    tickets = c.fetchall()

    return tickets


def query_router_status(conn: sqlite3.Connection, serial: str) -> list | None:
    """
    Get status of individual router
    :param conn: DB Connection object
    :param serial: Router serial
    :return: Router status
    """

    c = conn.cursor()

    c.execute("""SELECT status
              FROM routers
              WHERE serial = ?""",
              (serial,))
    router_status = c.fetchall()

    return router_status if len(router_status) > 0 else None


def query_switch_status(conn: sqlite3.Connection, serial: str) -> list | None:
    """
    Get status of individual switch
    :param conn: DB Connection object
    :param serial: Switch serial
    :return: Switch status
    """
    c = conn.cursor()

    c.execute("""SELECT status
              FROM switches
              WHERE serial = ?""",
              (serial,))
    switch_status = c.fetchall()

    return switch_status if len(switch_status) > 0 else None


def query_camera_status(conn: sqlite3.Connection, serial: str) -> list | None:
    """
    Get status of individual camera
    :param conn: DB Connection object
    :param serial: Camera serial
    :return: Camera status
    """
    c = conn.cursor()

    c.execute("""SELECT status
              FROM cameras
              WHERE serial = ?""",
              (serial,))
    camera_status = c.fetchall()

    return camera_status if len(camera_status) > 0 else None


def query_switch_connection(conn: sqlite3.Connection, serial: str) -> list | None:
    """
    Get connection for individual switch
    :param conn: DB Connection object
    :param serial: Switch serial
    :return: Switch connection
    """
    c = conn.cursor()

    c.execute("""SELECT connection
              FROM switches
              WHERE serial = ?""",
              (serial,))

    connection = c.fetchall()

    return connection if len(connection) > 0 else None


def query_camera_connection(conn: sqlite3.Connection, serial: str) -> list | None:
    """
    Get connection for individual camera
    :param conn: DB Connection object
    :param serial: Camera serial
    :return: Camera connection
    """
    c = conn.cursor()

    c.execute("""SELECT connection
              FROM cameras
              WHERE serial = ?""",
              (serial,))

    connection = c.fetchall()

    return connection if len(connection) > 0 else None


def update_device_status(conn: sqlite3.Connection, device_type: str, serial: str, status: str):
    """
    Update status of device
    :param conn: DB Connection object
    :param device_type: Type of device, dictates table
    :param serial: Device Serial
    :param status: Device new status
    """
    c = conn.cursor()

    if device_type == "router":
        table = "routers"
    elif device_type == "switch":
        table = "switches"
    elif device_type == "camera":
        table = "cameras"
    else:
        print("Unable to update device status because device type is not recognized")
        return

    update_statement = "UPDATE " + table + " SET status = '" + status + "' WHERE serial = ?"
    c.execute(update_statement, (serial,))
    conn.commit()


def query_specific_switch(conn: sqlite3.Connection, serial: str) -> list:
    """
    Return specific switch via serial number
    :param conn: DB Connection object
    :param serial: Switch serial
    :return: Switch entry
    """
    c = conn.cursor()

    c.execute("""SELECT serial
              FROM switches
              WHERE serial = ?""",
              (serial,))

    switch = c.fetchall()

    return switch


def query_specific_router(conn: sqlite3.Connection, serial: str) -> list:
    """
    Return specific router via serial number
    :param conn: DB Connection object
    :param serial: Router serial
    :return: Router entry
    """
    c = conn.cursor()

    c.execute("""SELECT serial
              FROM routers
              WHERE serial = ?""",
              (serial,))

    router = c.fetchall()

    return router


def query_specific_snow_ticket(conn: sqlite3.Connection, serial: str) -> list:
    """
    Return specific SNOW ticket associated with device serial number
    :param conn: DB Connection object
    :param serial: Device serial number
    :return: SNOW Incident ID
    """
    c = conn.cursor()

    c.execute("""SELECT incident_sys_id FROM snow_tickets WHERE serial = ?""", (serial,))

    ticket = c.fetchall()

    return ticket


def query_connected_switches_to_router(conn: sqlite3.Connection, serial: str) -> list:
    """
    Return downstream switches connected to router
    :param conn: DB Connection object
    :param serial: Router serial
    :return: Connected Switches
    """
    c = conn.cursor()

    c.execute("""SELECT serial
              FROM switches
              WHERE connection = ?""",
              (serial,))

    switches = c.fetchall()

    return switches


def query_connected_cameras_to_switches(conn: sqlite3.Connection, serial: str) -> list:
    """
    Return downstream cameras connected to switch
    :param conn: DB Connection object
    :param serial: Switch serial
    :return: Connected Cameras
    """
    c = conn.cursor()

    c.execute("""SELECT serial
              FROM cameras
              WHERE connection = ?""",
              (serial,))

    cameras = c.fetchall()

    return cameras


def add_router(conn: sqlite3.Connection, serial: str, status: str):
    """
    Add new router or update existing router in DB
    :param conn: DB Connection object
    :param serial: New router serial
    :param status: Router current status
    """
    c = conn.cursor()

    c.execute("""INSERT OR REPLACE INTO routers (serial, status)
              VALUES (?, ?)""",
              (serial, status))

    conn.commit()


def add_switch(conn: sqlite3.Connection, serial: str, status: str, connection=None):
    """
    Add new switch or update existing switch in DB
    :param connection: Upstream MX connection
    :param conn: DB Connection object
    :param serial: New switch serial
    :param status: Switch current status
    """
    c = conn.cursor()

    if connection is None:
        c.execute("""INSERT OR REPLACE INTO switches (serial, status)
                  VALUES (?, ?)""",
                  (serial, status))
    else:
        c.execute("""INSERT OR REPLACE INTO switches (serial, status, connection)
                  VALUES (?, ?, ?)""",
                  (serial, status, connection))

    conn.commit()


def add_camera(conn: sqlite3.Connection, serial: str, status: str, connection=None):
    """
    Add new camera or update existing camera in DB
    :param connection: Upstream switch connection
    :param conn: DB Connection object
    :param serial: New camera serial
    :param status: Camera current status
    """
    c = conn.cursor()

    if connection is None:
        c.execute("""INSERT OR REPLACE INTO cameras (serial, status)
                  VALUES (?, ?)""",
                  (serial, status))
    else:
        c.execute("""INSERT OR REPLACE INTO cameras (serial, status, connection)
                  VALUES (?, ?, ?)""",
                  (serial, status, connection))

    conn.commit()


def add_snow_ticket(conn: sqlite3.Connection, serial: str, incident_sys_id: str):
    """
    Add new SNOW ticket or update existing SNOW Ticket in DB
    :param conn: DB Connection object
    :param serial: Device serial number
    :param incident_sys_id: SNOW Incident ID
    """
    c = conn.cursor()

    c.execute("""INSERT OR REPLACE INTO snow_tickets (serial, incident_sys_id)
              VALUES (?, ?)""", (serial, incident_sys_id))

    conn.commit()


def delete_switch(conn: sqlite3.Connection, serial: str):
    """
    Delete switch from DB
    :param conn: DB Connection object
    :param serial: Device serial number
    """
    c = conn.cursor()

    c.execute("""DELETE from switches WHERE serial = ?""",
              (serial,))

    conn.commit()


def delete_router(conn: sqlite3.Connection, serial: str):
    """
    Delete router from DB
    :param conn: DB Connection object
    :param serial: Device serial number
    """
    c = conn.cursor()

    c.execute("""DELETE from routers WHERE serial = ?""",
              (serial,))

    conn.commit()


def delete_camera(conn: sqlite3.Connection, serial: str):
    """
    Delete camera from DB
    :param conn: DB Connection object
    :param serial: Device serial number
    """
    c = conn.cursor()

    c.execute("""DELETE from cameras WHERE serial = ?""",
              (serial,))

    conn.commit()


def delete_snow_ticket(conn: sqlite3.Connection, serial: str):
    """
    Delete SNOW Ticket from DB
    :param conn: DB Connection object
    :param serial: Device serial number
    """
    c = conn.cursor()

    c.execute("""DELETE from snow_tickets WHERE serial = ?""",
              (serial,))

    conn.commit()


def close_connection(conn: sqlite3.Connection):
    """
    Close DB Connection
    :param conn: DB Connection
    """
    conn.close()


# if running this python file, create connection to database, create tables, and print out the results of queries of
# every table
if __name__ == "__main__":
    conn = create_connection("sqlite.db")
    create_tables(conn)
    pprint(query_all_routers(conn))
    pprint(query_all_switches(conn))
    pprint(query_all_cameras(conn))
    close_connection(conn)
