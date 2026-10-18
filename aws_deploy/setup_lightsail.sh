#!/usr/bin/env bash
# ==============================================================================
# AWS Lightsail 24/7 Setup Script for DSPy Multi-Agent Telegram Employee
# Target OS: Ubuntu 24.04 LTS / Ubuntu 22.04 LTS
# ==============================================================================

set -e

echo "🚀 [1/6] Updating system packages..."
sudo apt-get update -y && sudo apt-get upgrade -y
sudo apt-get install -y python3-pip python3-venv git nginx curl ufw

echo "🔒 [2/6] Configuring Firewall (UFW)..."
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable || true

PROJECT_DIR="/home/ubuntu/dspy-multi-agents"

echo "📂 [3/6] Setting up project directory at $PROJECT_DIR..."
if [ ! -d "$PROJECT_DIR" ]; then
    echo "⚠️ Project directory not found. Please clone your repo or copy files here."
    exit 1
fi

cd "$PROJECT_DIR"

echo "🐍 [4/6] Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "⚙️ [5/6] Installing Systemd 24/7 Service..."
sudo cp aws_deploy/dspy-agent.service /etc/systemd/system/dspy-agent.service
sudo systemctl daemon-reload
sudo systemctl enable dspy-agent
sudo systemctl restart dspy-agent

echo "🌐 [6/6] Configuring Nginx Reverse Proxy..."
sudo cp aws_deploy/nginx.conf /etc/nginx/sites-available/dspy-agent
sudo ln -sf /etc/nginx/sites-available/dspy-agent /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

echo "=============================================================================="
echo "🎉 DEPLOYMENT COMPLETE! YOUR AI EMPLOYEE IS RUNNING 24/7 ON AWS!"
echo "Check bot service status:  sudo systemctl status dspy-agent"
echo "View real-time bot logs :  sudo journalctl -u dspy-agent -f"
echo "=============================================================================="
