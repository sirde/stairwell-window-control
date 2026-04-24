#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import socket               
import pycrc
import time
 
s = socket.socket()         # creat socket 
host = '192.168.8.200'        # set ip
port = 4196                 # Set port
 
cmd = [0, 0, 0, 0, 0, 0, 0, 0] 

s.connect((host, port))     # connect serve
while True:
    for i in range(8):
        cmd[0] = 0x01  #Device address
        cmd[1] = 0x05  #command
        cmd[2] = 0
        cmd[3] = i
        cmd[4] = 0xFF
        cmd[5] = 0
        crc = pycrc.ModbusCRC(cmd[0:6])
        cmd[6] = crc & 0xFF
        cmd[7] = crc >> 8
        #print(cmd)
        s.send(bytearray(cmd))
        time.sleep(0.2)
        data = s.recv(1024)
        print('[{}]'.format(','.join(hex(x) for x in data)))

        
    for i in range(8):
        cmd[0] = 0x01  #Device address
        cmd[1] = 0x05  #command
        cmd[2] = 0
        cmd[3] = i
        cmd[4] = 0
        cmd[5] = 0
        crc = pycrc.ModbusCRC(cmd[0:6])
        cmd[6] = crc & 0xFF
        cmd[7] = crc >> 8
        #print(cmd)
        s.send(bytearray(cmd))
        time.sleep(0.2)
        data = s.recv(1024)
        print('[{}]'.format(','.join(hex(x) for x in data)))
        
    cmd[0] = 0x01  #Device address
    cmd[1] = 0x02  #command
    cmd[2] = 0
    cmd[3] = 0
    cmd[4] = 0
    cmd[5] = 8
    crc = pycrc.ModbusCRC(cmd[0:6])
    cmd[6] = crc & 0xFF
    cmd[7] = crc >> 8
    s.send(bytearray(cmd))
    time.sleep(0.2)
    data = s.recv(1024)
    # print('[{}]'.format(','.join(hex(x) for x in data)))
    print("Input status :" + str(hex(data[3])))
    
s.close()                   # Close the connection