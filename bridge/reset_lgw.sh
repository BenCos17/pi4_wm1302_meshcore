#!/bin/bash

# GPIO mapping for SenseCAP M1 Shield
SX1302_RESET_PIN=17
SX1302_POWER_EN=18

echo "SenseCAP M1: Resetting SX1302 on Pin $SX1302_RESET_PIN..."

# Force Power ON (GPIO 18)
pinctrl set $SX1302_POWER_EN op dh

# Perform Reset Pulse (GPIO 17 is Active High for Reset on some, Active Low for others)
# This sequence ensures a clean transition
pinctrl set $SX1302_RESET_PIN op dl
sleep 0.1
pinctrl set $SX1302_RESET_PIN op dh
sleep 0.2
pinctrl set $SX1302_RESET_PIN op dl
sleep 0.5

echo "SenseCAP M1: Reset complete."
exit 0