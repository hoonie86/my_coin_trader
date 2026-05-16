kill $(ps -ef | grep 'rocky' | grep 'main.py' | grep -v 'grep' | awk '{print $2}') 2>/dev/null
