import os


# Window structure per building
BUILDINGS = {
    "building_1": ["A"],
    "building_3": ["A", "B", "D", "E"],
    "building_5": ["A", "B", "D", "E"]
}

# IP address of the Modbus-TCP relay module for each building.
module_address = {
    "building_1": os.environ["RELAY_IP_BUILDING_1"],
    "building_3": os.environ["RELAY_IP_BUILDING_3"],
    "building_5": os.environ["RELAY_IP_BUILDING_5"],
}

# Buildings that actually have the relay module wired up. Others are shown
# in the UI but disabled. Set via EQUIPPED_BUILDINGS, comma-separated.
EQUIPPED_BUILDINGS = {
    b.strip()
    for b in os.environ["EQUIPPED_BUILDINGS"].split(",")
    if b.strip()
}