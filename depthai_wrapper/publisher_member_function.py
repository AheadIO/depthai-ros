# Copyright 2016 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import rclpy
import threading
import msgpack
import msgpack_numpy as m
import numpy as np

from rclpy.node import Node
from std_msgs.msg import ByteMultiArray, String, UInt8MultiArray
from rcl_interfaces.msg import ParameterDescriptor
from rcl_interfaces.msg import ParameterType


# get path to DepthAI stuff.
import sys
#sys.path.append("/home/jngai/Desktop/depthai/")

# pull in the depthai stuff.
import depthai
import cv2
import json

from time import time, sleep, monotonic

from depthai_helpers import utils
from depthai_helpers.cli_utils import cli_print, PrintColors

from depthai_helpers.object_tracker_handler import show_tracklets

from depthai_helpers.config_manager import DepthConfigManager
from depthai_helpers.arg_manager import CliArgs



#-------------------------------------------------------
# TODO: should move the watchdog stuff to a shared class
#-------------------------------------------------------
process_watchdog_timeout=10 #seconds
def reset_process_wd():
    global wd_cutoff
    wd_cutoff=monotonic()+process_watchdog_timeout
    return
#-------------------------------------------------------


class DepthAIPublisher(Node):
    def thread_function(self, name):
        device = None
        if self.debug_mode:
            print('Cmd file: ', self.cmd_file, ' args["device_id"]: ', self.args['device_id'])
            device = depthai.Device(self.cmd_file, self.args['device_id'])
        else:
            device = depthai.Device(self.args['device_id'], self.usb2_mode)

        print('Available streams: ' + str(device.get_available_streams()))

        # create the pipeline, here is the first connection with the device
        self.pipeline = device.create_pipeline(config=self.config)

        if self.pipeline is None:
            print('Pipeline is not created.')
            exit(3)


        # add nn2depth to a parameter so clients can get at it to decode depth?
        # setup a param for sharing the depth mapping for this particular device.
        nn2depth = device.get_nn_to_depth_bbox_mapping()
        nn2depthStr = json.dumps(nn2depth)
        self.nn2depthParamName = "nn2depth"+self.targetDev
        paramDefault = ""
        paramDesc = ParameterDescriptor(type=ParameterType.PARAMETER_STRING,
                                              description="Used ")
        self.declare_parameter(self.nn2depthParamName, nn2depthStr, paramDesc)


        t_start = time()
        frame_count = {}
        frame_count_prev = {}
        nnet_prev = {}
        nnet_prev["entries_prev"] = {}
        nnet_prev["nnet_source"] = {}
        frame_count['nn'] = {}
        frame_count_prev['nn'] = {}

        NN_cams = {'rgb', 'left', 'right'}

        for cam in NN_cams:
            nnet_prev["entries_prev"][cam] = []
            nnet_prev["nnet_source"][cam] = []
            frame_count['nn'][cam] = 0
            frame_count_prev['nn'][cam] = 0
        #-------------------------------------------------------


        stream_windows = []
        for s in self.stream_names:
            if s == 'previewout':
                for cam in NN_cams:
                    stream_windows.append(s + '-' + cam)
            else:
                stream_windows.append(s)

        for w in stream_windows:
            frame_count[w] = 0
            frame_count_prev[w] = 0

        tracklets = None


        #-------------------------------------------------------
        # start watchdog
        reset_process_wd()
        #-------------------------------------------------------

        while True:
            # retreive data from the device
            # data is stored in packets, there are nnet (Neural NETwork) packets which have additional functions for NNet result interpretation
            nnet_packets, data_packets = self.pipeline.get_available_nnet_and_data_packets(True)

            #-------------------------------------------------------
            # TODO: should move the watchdog stuff to a shared class
            #-------------------------------------------------------
            packets_len = len(nnet_packets) + len(data_packets)
            if packets_len != 0:
                reset_process_wd()
            else:
                cur_time=monotonic()
                if cur_time > wd_cutoff:
                    print("process watchdog timeout")
                    os._exit(10)
            #-------------------------------------------------------

            for _, nnet_packet in enumerate(nnet_packets):
                meta = nnet_packet.getMetadata()
                camera = 'rgb'
                if meta != None:
                    camera = meta.getCameraName()

                serializedEntry = self.decode_nn(nnet_packet, config=self.config, NN_json=self.NN_json)

                self.nnmsg.data = str(serializedEntry)
                self.nnResultPublisher.publish(self.nnmsg)
                
                frame_count['metaout'] += 1
                frame_count['nn'][camera] += 1


            for packet in data_packets:
                window_name = packet.stream_name
                if packet.stream_name not in self.stream_names:
                    continue # skip streams that were automatically added
                packetData = packet.getData()
                if packetData is None:
                    print('Invalid packet data!')
                    continue
                elif packet.stream_name == 'previewout':
                    #broadcast to previewout
                    meta = packet.getMetadata()
                    camera = 'rgb'
                    if meta != None:
                        camera = meta.getCameraName()

                    window_name = 'previewout-' + camera
                    # the format of previewout image is CHW (Chanel, Height, Width), but OpenCV needs HWC, so we
                    # change shape (3, 300, 300) -> (300, 300, 3)
                    data0 = packetData[0,:,:]
                    data1 = packetData[1,:,:]
                    data2 = packetData[2,:,:]
                    frame = cv2.merge([data0, data1, data2])

                    self.publishFrame(frame, self.previewPublisher, self.previewmsg)

                elif packet.stream_name == 'left':
                    frame_bgr = packetData
                    self.publishFrame(frame_bgr, self.leftPublisher, self.leftmsg)
                elif packet.stream_name == 'right':
                    frame_bgr = packetData
                    self.publishFrame(frame_bgr, self.rightPublisher, self.rightmsg)
                elif packet.stream_name == 'disparity':
                    frame_bgr = packetData
                    self.publishFrame(frame_bgr, self.disparityPublisher, self.disparitymsg)

                elif packet.stream_name.startswith('depth'):
                    frame = packetData
                    self.publishFrame(frame, self.depthPublisher, self.depthmsg)

                # TODO: maybe just publish the filepath?
                elif packet.stream_name == 'jpegout':
                    jpg = packetData
                    mat = cv2.imdecode(jpg, cv2.IMREAD_COLOR)
                    cv2.imshow('jpegout', mat)

                elif packet.stream_name == 'video':
                    videoFrame = packetData
                    videoFrame.tofile(video_file)
                    #mjpeg = packetData
                    #mat = cv2.imdecode(mjpeg, cv2.IMREAD_COLOR)
                    #cv2.imshow('mjpeg', mat)

                elif packet.stream_name == 'meta_d2h':
                    str_ = packet.getDataAsStr()
                    self.d2hmsg.data = str_
                    self.d2hPublisher.publish(self.d2hmsg)

                elif packet.stream_name == 'object_tracker':
                    tracklets = packet.getObjectTracker()

                frame_count[window_name] += 1

            key = cv2.waitKey(1)

    def publishFrame(self, frame, publisher, msg):
        serializedFrame = msgpack.packb(frame, default=m.encode)
        intarr = list(serializedFrame)

        #start = time()
        # this takes 0.04 seconds for a 300x300 image. That's way too slow. 
        # Oh, this is actually because __debug__ is enabled and it's importing a bunch of stuff inline... This should be fixed on ros2's side or we need a way to disable __debug__.
        msg.data = intarr
        #end = time()
        #print(end - start)

        publisher.publish(msg)


    def __init__(self):
        super().__init__('depthai_publisher')

        # setup a cli param for passing in command line arguments.
        self.cliParamName = "cliArgs"
        paramDefault = ""
        paramDesc = ParameterDescriptor(type=ParameterType.PARAMETER_STRING,
                                              description="arguments that match the command line script depthai-demo.py input")
        self.declare_parameter(self.cliParamName, paramDefault, paramDesc)

        # get cli param
        my_param = self.get_parameter(self.cliParamName).get_parameter_value().string_value
        new_argv = [sys.argv[0]]
        if my_param is not "":
            paramList = my_param.split(" ")
            for param in paramList:
                new_argv.append(param)

        # setup argv for parsing.
        self.old_argv = sys.argv
        sys.argv = new_argv

        # parse params
        cliArgs = CliArgs()
        self.args = vars(cliArgs.parse_args())
        print(self.args)

        configMan = DepthConfigManager(self.args)
        self.cmd_file, self.debug_mode = configMan.getCommandFile()
        self.usb2_mode = configMan.getUsb2Mode()
        self.decode_nn = configMan.decode_nn
        self.decode_nn_json = configMan.decode_nn_json
        self.show_nn = configMan.show_nn
        self.labels = configMan.labels
        self.NN_json = configMan.NN_config

        # This json file is sent to DepthAI. It communicates what options you'd like to enable and what model you'd like to run.
        self.config = configMan.jsonConfig

        self.targetDev = self.args['device_id'].replace(".", "_")

        # setup publishers
        self.previewPublisher = self.create_publisher(UInt8MultiArray, 'preview'+self.targetDev, 10)
        self.previewmsg = UInt8MultiArray()

        self.leftPublisher = self.create_publisher(UInt8MultiArray, 'left'+self.targetDev, 10)
        self.leftmsg = UInt8MultiArray()

        self.rightPublisher = self.create_publisher(UInt8MultiArray, 'right'+self.targetDev, 10)
        self.rightmsg = UInt8MultiArray()

        self.disparityPublisher = self.create_publisher(UInt8MultiArray, 'disparity'+self.targetDev, 10)
        self.disparitymsg = UInt8MultiArray()

        self.depthPublisher = self.create_publisher(UInt8MultiArray, 'depth'+self.targetDev, 10)
        self.depthmsg = UInt8MultiArray()

        self.d2hPublisher = self.create_publisher(String, 'd2h'+self.targetDev, 10)
        self.d2hmsg = String()

        self.nnResultPublisher = self.create_publisher(String, 'meta'+self.targetDev, 10)
        self.nnmsg = String()

        # Create a list of enabled streams ()
        self.stream_names = [stream if isinstance(stream, str) else stream['name'] for stream in configMan.stream_list]
        self.enable_object_tracker = 'object_tracker' in self.stream_names




def main(args=None):
    rclpy.init(args=args)
    print('depthai.__version__ == %s' % depthai.__version__)
    depthai_publisher = DepthAIPublisher()

    # start the main processing loop that grabs packets from the pipeline and publishes them.
    x = threading.Thread(target=depthai_publisher.thread_function, args=(1,))
    x.start()

    rclpy.spin(depthai_publisher)

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    depthai_publisher.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
