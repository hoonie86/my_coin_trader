kill $(ps -ef | grep 'rocky' | grep 'main.py' | grep -v 'grep' | awk '{print $2}') 2>/dev/null
nohup python3 /home/rocky/my_coin_trader/main.py > /dev/null 2>&1 &
