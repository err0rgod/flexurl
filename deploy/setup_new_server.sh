#!/bin/bash

echo "🚀 Setting up new FlexURL server..."

# 1. Install Dependencies
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv redis-server postgresql postgresql-contrib nginx git

# 2. Start Redis
sudo systemctl start redis-server
sudo systemctl enable redis-server

# 3. Setup Project Directory (assuming user cloned the repo to /home/ubuntu/urlshortener)
cd /home/ubuntu/urlshortener
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Copy Nginx Configuration
sudo cp deploy/nginx-urlshortener /etc/nginx/sites-available/urlshortener
sudo ln -sf /etc/nginx/sites-available/urlshortener /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo systemctl restart nginx

# 5. Copy Systemd Services
sudo cp deploy/urlshortener.service /etc/systemd/system/
sudo cp deploy/flexurl-worker.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable urlshortener.service
sudo systemctl enable flexurl-worker.service

echo "✅ Server dependencies and services configured!"
echo "⚠️  IMPORTANT REMINDERS ⚠️"
echo "1. Create your .env file at /home/ubuntu/urlshortener/.env"
echo "2. Set up your PostgreSQL database (tables must be created)"
echo "3. Run 'sudo systemctl start urlshortener.service' and 'sudo systemctl start flexurl-worker.service' when ready."
