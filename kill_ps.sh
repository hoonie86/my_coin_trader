kill $(ps -ef | grep 'main.py' | grep -v 'grep' | awk '{print $2}')
