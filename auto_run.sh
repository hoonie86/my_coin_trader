kill $(ps -ef | grep 'main.py' | grep -v 'grep' | awk '{print $2}') 2>/dev/null
nohup python3 main.py > /dev/null 2>&1 &