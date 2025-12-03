# music-downloader.service file is used to create a service in the path: /etc/systemd/system folder which will help to automatically run the web app at startup.

#main.py is the backend code to download songs.
# index.html & static folder creates the frontend web page
# in the downloades folder, songs are getting downloaded. 

# to use it, first setup venv for python & then activate the venv and run the python script; Python3 main.py 

***********************************************
# Go to the music downloader app folder
$ cd /path/to/app/folder

# Install the venv for python3
$ sudo apt install python3.12-venv

# Create the venv in the application folder
$ python3 -m venv venv

# Activate the venv
$ source venv/bin/activate

# Install the requirements
$ pip install fastapi uvicorn yt-dlp jinja2 python-multipart


# To run the script
$ python3 main.py
$ sudo apt update
$ sudo apt install ffmpeg

# Run the Script
$ python3 main.py

# to deactivate the venv
$ deactivate 
*************************************************
# To make it capable to for startup run:
# Move the music-downloader.service file in below path

$ /etc/systemd/system

# reaload deamon:
$ sudo systemctl deamon-reload

# enable the service
$ sudo systemctl enable music-downloader.service

# Start the service
$ sudo systemctl start music-downloader.service

# Reboot the system
$ sudo reboot
