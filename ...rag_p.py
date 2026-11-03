from math import log
import os
from pathlib import Path
from agentic_sdk.configloader import ConfigLoader
import logging

from agent.billing_agent import BillingAgent


log = logging.getLogger(__name__)


def main():
    config_path = r"C:\Users\T00934\Desktop\billing_agent\config\app.yaml"
    log.info(f"Loading configuration from: {config_path}")
    ConfigLoader.load_from_filesystem(config_path)

    path_setting = ConfigLoader.get_agent_config(BillingAgent)
    print(path_setting)


if __name__ == "__main__":
    main()
