# The meter model shelf

Fits made at deployment, one subdirectory per detector (`meter_iforest/`, `meter_lof/`), found
through `IOTSHIELD_METER_MODELS_PATH` (ADR-0048 D3). Each `<name>.json` is offered by name in a
detector's `artifact` param and in the dashboard's artifact dropdown.

    uv run python -m iotshield.detect.meter.train --name <fleet> \
        --source <clean export> --source-format sensipro-hourly-csv

A fit stores the readings it learned from, so these files are operator data and are gitignored.
