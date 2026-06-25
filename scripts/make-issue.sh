#!/bin/bash
set -e

# Get the directory of the script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Create a virtual environment in scripts/.venv if it doesn't exist
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating python virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
    echo "Installing google-antigravity SDK..."
    "$VENV_DIR/bin/pip" install --quiet google-antigravity
fi

# Run the python script
"$VENV_DIR/bin/python3" "$SCRIPT_DIR/make_issue.py" "$@"
