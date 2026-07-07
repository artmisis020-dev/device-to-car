# Wireguad Setup step

## Install required deps
sudo apt install wireguard wireguard-tools resolvconf

## Add symlink for start on reboot
sudo systemctl enable wg-quick@wg0

## Start wervice
sudo systemctl start wg-quick@wg0

## Check status after start
sudo systemctl status wg-quick@wg0 --no-pager