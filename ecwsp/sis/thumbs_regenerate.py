#!/usr/bin/python

import sys
from django.core.management import setup_environ
from django.conf import settings

# Put the path for your Django installation here.
sys.path.append('/home/vinicius/web/erp/')

setup_environ(settings)
from main.thumbs import *
regenerate_thumbs()
