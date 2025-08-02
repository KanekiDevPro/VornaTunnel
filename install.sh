#!/bin/bash

set -e

echo "🔄 Updating system and installing dependencies..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv socat curl

echo "📦 Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "⬆️ Upgrading pip and installing Python packages..."
pip install --upgrade pip
pip install requests colorama

echo "🌐 Downloading vorna.py from GitHub..."
curl -O https://raw.githubusercontent.com/iliya-Developer/VornaTunnel/main/vorna.py

echo "🚀 Running vorna.py..."
python vorna.py