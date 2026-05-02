#!/bin/bash
pip install -r requirements.txt
cd modules && g++ -O2 -shared -fPIC -o data_engine.so data_engine.cpp && cd ..
