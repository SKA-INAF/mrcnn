"""
Mask R-CNN
Train on the radio galaxy/source/sidelobe dataset.

Copyright (c) 2020 Simone Riggi - INAF
Licensed under the GPL3 License (see LICENSE for details)

------------------------------------------------------------

Usage: Run from the command line as such:

    # Train a new model starting from pre-trained weights
    python3 train.py train --dataset=/path/to/dataset --weights=...

"""

import os
import sys
import json
import time
import argparse
import datetime
import numpy as np
import skimage.draw
import skimage.measure
import tensorflow as tf
from imgaug import augmenters as iaa
from skimage.measure import find_contours
from imgaug import augmenters as iaa
import uuid

# Root directory of the project
ROOT_DIR = os.getcwd()

# Import Mask RCNN
sys.path.append(ROOT_DIR)  # To find local version of the library
from mrcnn import logger
from mrcnn.config import Config
from mrcnn import model as modellib, utils
from mrcnn import visualize
from mrcnn.analyze import ModelTester
from mrcnn.analyze import Analyzer
from mrcnn.graph import Graph

## Import graphics modules
import matplotlib
#matplotlib.use('Agg')
#matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib import patches, lines
from matplotlib.patches import Polygon

# Directory to save logs and model checkpoints, if not provided
# through the command line argument --logs
DEFAULT_LOGS_DIR = os.path.join(ROOT_DIR, "logs")

# Suppress `Invalid 'BLANK' keyword in header.` warnings
import warnings
from astropy.io.fits.verify import VerifyWarning
warnings.simplefilter('ignore', category=VerifyWarning)

############################################################
#  Configurations
############################################################


class SDetectorConfig(Config):
    
	""" Configuration for training on the toy  dataset.
			Derives from the base Config class and overrides some values.
	"""
	# Give the configuration a recognizable name	
	NAME = "rg-dataset"

	# NUMBER OF GPUs to use. When using only a CPU, this needs to be set to 1.
	GPU_COUNT = 1

	# We use a GPU with 12GB memory, which can fit two images.
	# Adjust down if you use a smaller GPU.
	IMAGES_PER_GPU = 2

	# Number of classes (including background)
	##NUM_CLASSES = 1 + 5  # Background + Objects (sidelobes, sources, galaxy_C1, galaxy_C2, galaxy_C3)
	##NUM_CLASSES = 1 + 3  # Background + Objects (sidelobes, sources, galaxy)
	NUM_CLASSES = 1
	CLASS_NAMES = ["bkg"]

	# Number of training steps per epoch
	#STEPS_PER_EPOCH = 16000
	VALIDATION_STEPS = max(1, 200 // (IMAGES_PER_GPU*GPU_COUNT)) # 200 validation/test images
	STEPS_PER_EPOCH = ((16439 - 200) // (IMAGES_PER_GPU*GPU_COUNT)) #16439 total images
	#STEPS_PER_EPOCH = ((18888 - 200) // (IMAGES_PER_GPU*GPU_COUNT)) #18888 total images
	
	# Don't exclude based on confidence. Since we have two classes
	# then 0.5 is the minimum anyway as it picks between source and BG
	DETECTION_MIN_CONFIDENCE = 0 # default=0.9 (skip detections with <90% confidence)

	# Non-maximum suppression threshold for detection
	DETECTION_NMS_THRESHOLD = 0.3

	# Length of square anchor side in pixels
	RPN_ANCHOR_SCALES = (4,8,16,32,64)

	# Maximum number of ground truth instances to use in one image
	MAX_GT_INSTANCES = 300 # default=100

	# Use a smaller backbone
	BACKBONE = "resnet101"

	# The strides of each layer of the FPN Pyramid. These values
	# are based on a Resnet101 backbone.
	BACKBONE_STRIDES = [4, 8, 16, 32, 64]
	
	# Input image resizing
	# Generally, use the "square" resizing mode for training and predicting
	# and it should work well in most cases. In this mode, images are scaled
	# up such that the small side is = IMAGE_MIN_DIM, but ensuring that the
	# scaling doesn't make the long side > IMAGE_MAX_DIM. Then the image is
	# padded with zeros to make it a square so multiple images can be put
	# in one batch.
	# Available resizing modes:
	# none:   No resizing or padding. Return the image unchanged.
	# square: Resize and pad with zeros to get a square image
	#         of size [max_dim, max_dim].
	# pad64:  Pads width and height with zeros to make them multiples of 64.
	#         If IMAGE_MIN_DIM or IMAGE_MIN_SCALE are not None, then it scales
	#         up before padding. IMAGE_MAX_DIM is ignored in this mode.
	#         The multiple of 64 is needed to ensure smooth scaling of feature
	#         maps up and down the 6 levels of the FPN pyramid (2**6=64).
	# crop:   Picks random crops from the image. First, scales the image based
	#         on IMAGE_MIN_DIM and IMAGE_MIN_SCALE, then picks a random crop of
	#         size IMAGE_MIN_DIM x IMAGE_MIN_DIM. Can be used in training only.
	#         IMAGE_MAX_DIM is not used in this mode.
	IMAGE_RESIZE_MODE = "square"
	IMAGE_MIN_DIM = 256
	IMAGE_MAX_DIM = 256
	
	# Image mean (RGB)
	#MEAN_PIXEL = np.array([112,112,112])
	# Image mean (RGB) - consider setting these to zero, and do per image mean/std normalization
	MEAN_PIXEL = np.array([0, 0, 0])

	# Non-max suppression threshold to filter RPN proposals.
	# You can increase this during training to generate more propsals.
	RPN_NMS_THRESHOLD = 0.9 # default=0.7

	# How many anchors per image to use for RPN training
	RPN_TRAIN_ANCHORS_PER_IMAGE = 512  # default=128

	# Number of ROIs per image to feed to classifier/mask heads	
	# The Mask RCNN paper uses 512 but often the RPN doesn't generate
	# enough positive proposals to fill this and keep a positive:negative
	# ratio of 1:3. You can increase the number of proposals by adjusting
	# the RPN NMS threshold.
	TRAIN_ROIS_PER_IMAGE = 512


	# Ratios of anchors at each cell (width/height)
	# A value of 1 represents a square anchor, and 0.5 is a wide anchor
	RPN_ANCHOR_RATIOS = [0.5, 1, 2]

	## Learning rate and momentum
	## The Mask RCNN paper uses lr=0.02, but on TensorFlow it causes
	## weights to explode. Likely due to differences in optimizer
	## implementation.
	LEARNING_RATE = 0.0005
	# LEARNING_MOMENTUM = 0.9
	OPTIMIZER = "ADAM" # default is SGD

	# If enabled, resizes instance masks to a smaller size to reduce
	# memory load. Recommended when using high-resolution images.
	USE_MINI_MASK = False



############################################################
#       DATASET CLASS
############################################################

class SourceDataset(utils.Dataset):

	""" Define dataset class """

	# ================================================================
	# ==   CONSTRUCTOR
	# ================================================================
	def __init__(self):
		utils.Dataset.__init__(self)
	
		self.class_id_map= {}
		self.nclasses= 0
		self.loaded_imgs= 0
		self.convert_to_rgb= True
		
		
	# ================================================================
	# ==   INIT
	# ================================================================
	def set_class_dict(self, class_dict_str):
		""" Set class dictionary from json string """

		# - Check
		if class_dict_str=="":
			logger.error("Empty string given!")
			return -1

		# - Set class id dictionary
		logger.info("Set class id dictionary ...")
		class_dict= {}
		try:
			class_dict= json.loads(class_dict_str)
		except:
			logger.error("Failed to get dictionary from string!")
			return -1
		self.class_id_map= class_dict
		
		
		# - Reset class info (defined in parent class) and add new entries defined in dictionary
		#logger.info("Reset class info ...")
		#self.class_info= [{"source": "", "id": 0, "name": "BG"}]

		for class_name in self.class_id_map:
			class_id= self.class_id_map[class_name]
			self.add_class("rg-dataset", class_id, class_name)

		# - Append unknown class if not given
		#self.add_class("rg-dataset", -1, "unknown")

		#logger.info("class_info",self.class_info)
			
		# - Append bkg & unknown item (if not given in input)
		self.class_id_map['bkg']= 0
		#self.class_id_map['unknown']= -1

		# - Set number of classes
		self.nclasses= len(self.class_id_map)
		
		logger.debug("class_id_map=%s, nclasses=%d" % (str(self.class_id_map),self.nclasses))		
	

		return 0

	# ================================================================
	# ==   LOAD DATASET FROM IMAGE 
	# ================================================================
	def load_data_from_image(self, filename, filename_mask="", class_name="unknown"):
		""" Load data from image """
			
		# - Check image
		filename_fullpath= os.path.abspath(filename)
		image_id= str(uuid.uuid1())
		valid_img= (os.path.isfile(filename_fullpath) and filename_fullpath.endswith('.fits'))
	
		if not valid_img:
			logger.error("Image file %s does not exist or has unexpected extension (.fits required)" % filename)
			return -1

		# - Check mask
		have_mask= False
		path_masks= []
		if filename_mask!="":
			filename_mask_fullpath= os.path.abspath(filename_mask)
			if not valid_mask:
				logger.warn("Mask file %s does not exist or has unexpected extension (.fits required)" % filename_mask)
				return -1
			path_masks.append(filename_mask_fullpath)
			have_mask= True

		# - Check class id	
		class_ids= []
		if have_mask:
			if class_name in self.class_id_map:
				class_id= self.class_id_map.get(class_name)	
				class_ids.append(class_id)
			else:
				logger.error("Image file %s class name (%s) is not present in dictionary, skip it..." % (filename,class_name))
				return -1

		# - Add image
		self.add_image(
    	"rg-dataset",
			image_id=image_id,  # use file name as a unique image id
			path=filename_fullpath,
			path_masks=path_masks,
			class_ids=class_ids
		)

		self.loaded_imgs+= 1

		return 0

	# ================================================================
	# ==   LOAD DATASET FROM ASCII (row format: file,mask,class_id)
	# ================================================================
	def load_data_from_list(self, dataset, nmaximgs=-1):
		""" Load a subset of the source dataset.
				dataset_dir: Root directory of the dataset.
		"""
		 
		# - Read dataset
		img_counter= 0
		status= 0

		with open(dataset,'r') as f:
		
			for line in f:
				line_split = line.strip().split(',')
				(filename,filename_mask,class_name) = line_split

				# - Get and check paths
				filename_fullpath= os.path.abspath(filename)
				filename_mask_fullpath= os.path.abspath(filename_mask)
				image_id= str(uuid.uuid1())

				valid_img= (os.path.isfile(filename_fullpath) and filename_fullpath.endswith('.fits'))
				valid_mask= (os.path.isfile(filename_mask_fullpath) and filename_mask_fullpath.endswith('.fits'))
				if not valid_img:
					logger.warn("Image file %s does not exist or has unexpected extension (.fits required)" % filename)
					status= -1
					continue
				if not valid_mask:
					logger.warn("Mask file %s does not exist or has unexpected extension (.fits required)" % filename_mask)
					status= -1
					continue

				# - Add image				
				class_id= 0
				if class_name in self.class_id_map:
					class_id= self.class_id_map.get(class_name)	
				else:
					logger.warn("Image file %s class name (%s) is not present in dictionary, skip it..." % (filename,class_name))
					status= -1
					continue		

				logger.info("Adding image file %s (class=%s,id=%d)..." % (filename,class_name,class_id))		

				self.add_image(
        	"rg-dataset",
					#image_id=filename_base_noext,  # use file name as a unique image id
					image_id=image_id,  # use file name as a unique image id
					path=filename_fullpath,
					path_masks=[filename_mask_fullpath],
					class_ids=[class_id]
				)
				img_counter+= 1
				self.loaded_imgs+= 1
				if nmaximgs!=-1 and img_counter>=nmaximgs:
					logger.info("Max number (%d) of desired images reached, stop loading ..." % nmaximgs)
					break

		if status<0:
			logger.warn("One or more files have been skipped...")
		if img_counter<=0:
			logger.error("All files in list have been skipped!")		
			return -1
		logger.info("#%d images added in dataset..." % img_counter)

		return 0

	# ================================================================
	# ==   LOAD DATASET FROM JSON
	# ================================================================
	def load_data_from_json_file(self, filename, rootdir=''):
		""" Load dataset specified in a json file """

		# - Read json file
		try:
			json_file = open(filename)
		except IOError:
			logger.error("Failed to open file %s, skip it..." % filename)
			return -1
	
		# - Read obj info
		d= json.load(json_file)				
		#print(d)
					
		#img_path= d['img']
		img_path= os.path.join(rootdir, d['img'])
		img_fullpath= os.path.abspath(img_path)
		img_path_base= os.path.basename(img_fullpath)
		img_path_base_noext= os.path.splitext(img_path_base)[0]
		img_id= str(uuid.uuid1())
	
		logger.debug("img_path=%s, img_fullpath=%s" % (img_path,img_fullpath))

		valid_img= (os.path.isfile(img_fullpath) and img_fullpath.endswith('.fits'))
		if not valid_img:
			logger.warn("Image file %s does not exist or has unexpected extension (.fits required)" % img_fullpath)
			return -1
	
		nobjs= len(d['objs'])
		logger.debug("#%d objects present in file %s ..." % (nobjs,filename))
				
		mask_paths= []
		class_ids= []	
		good_masks= True
				
		for obj_dict in d['objs']:
			mask_path= os.path.join(rootdir,obj_dict['mask'])
			mask_fullpath= os.path.abspath(mask_path)
			valid_img= (os.path.isfile(mask_fullpath) and mask_fullpath.endswith('.fits'))
			if not valid_img:
				good_masks= False
				break

			class_name= obj_dict['class']
			class_id= 0
			if class_name in self.class_id_map:
				class_id= self.class_id_map.get(class_name)
			else:
				logger.warn("Image file %s class name (%s) is not present in dictionary, skip it..." % (img_fullpath,class_name))
				continue	

			mask_paths.append(mask_fullpath)
			class_ids.append(class_id)
				
		if not good_masks:
			logger.error("One or more mask of file %s does not exist or have unexpected extension (.fits required)" % img_fullpath)
			return -1
					
		# - Add image & mask informations in dataset class
		self.add_image(
    	"rg-dataset",
			image_id=img_id,
			path=img_fullpath,
			path_masks=mask_paths,
			class_ids=class_ids
		)
	
		return 0

	# ================================================================
	# ==   LOAD DATASET FROM ASCII (row format: jsonfile)
	# ================================================================
	def load_data_from_json_list(self, dataset, nmaximgs):
		""" Load dataset specified in a json filelist """
	
		# - Read json filelist
		img_counter= 0
		status= 0

		with open(dataset,'r') as f:
			for filename in f:
				logger.info("Loading dataset info from file %s ..." % filename)

				# - Load from json file
				status= self.load_data_from_json_file(filename)
				if status<0:
					continue

				img_counter+= 1
				self.loaded_imgs+= 1	
				if nmaximgs!=-1 and img_counter>=nmaximgs:
					logger.info("Max number (%d) of desired images reached, stop loading ..." % nmaximgs)
					break

		if status<0:
			logger.warn("One or more files have been skipped...")
		if img_counter<=0:
			logger.error("All files in list have been skipped!")		
			return -1
		logger.info("#%d images added in dataset..." % img_counter)

		return 0

	# ========================================================================
	# ==   LOAD DATASET FROM ASCII FOUND RECURSIVELY STARTING FROM TOPDIR
	# =========================================================================
	def load_data_from_json_search(self, topdir, nmaximgs):
		""" Load dataset found in json files recursively """
	
		# - Check topdir exists
		if not os.path.isdir(topdir):
			logger.error("Directory %d does not exists on filesystem!" % topdir)
			return -1			

		# - Traverse dir and search for json files
		img_counter= 0
		stop= False

		for root, dirs, files in os.walk(topdir):
			path = root.split(os.sep)
			#print((len(path) - 1) * '---', os.path.basename(root))
			#for file in files:
			for filename in sorted(files):
				if not filename.endswith(".json"):
					continue
				filename_fullpath= os.path.join(root, filename)
				#print(len(path) * '---', file)

				# - Load from json file
				status= self.load_data_from_json_file(filename_fullpath,root)
				if status<0:
					logger.warn("Failed to load data from file %s ..." % filename_fullpath)
					continue

				img_counter+= 1	
				self.loaded_imgs+= 1		
				if nmaximgs!=-1 and img_counter>=nmaximgs:
					logger.info("Max number (%d) of desired images reached, stop loading ..." % nmaximgs)
					stop= True
					break

			if stop:
				break


		return 0

	# ================================================================
	# ==   LOAD GT MASKS (multiple objects per image)
	# ================================================================
	def load_gt_masks(self, image_id, binary=True):
		""" Load gt mask """

		# Read filename
		info = self.image_info[image_id]
		filenames= info["path_masks"]
		nobjs= len(filenames)

		# Read mask file and fill binary mask images
		mask = None	
		counter= 0

		for filename in filenames:
			data, header= utils.read_fits(filename,stretch=False,normalize=False,convertToRGB=False)
			height= data.shape[0]
			width= data.shape[1]
			if binary:
				data= data.astype(np.bool)
			if mask is None:
				if binary:
					mask = np.zeros([height,width,nobjs],dtype=np.bool)
				else:
					mask = np.zeros([height,width,nobjs],dtype=np.int)
			mask[:,:,counter]= data
			counter+= 1
	
		return mask


	# ================================================================
	# ==   LOAD MASK (multiple objects per image)
	# ================================================================
	def load_mask(self, image_id):
		""" Generate instance masks for an image.
				Returns:
					masks: A bool array of shape [height, width, instance count] with one mask per instance.
					class_ids: a 1D array of class IDs of the instance masks.
		"""

		# - Check	dataset name
		if self.image_info[image_id]["source"] != "rg-dataset":
			return super(self.__class__, self).load_mask(image_id)

		# - Set bitmap mask of shape [height, width, instance_count]
		info = self.image_info[image_id]
		filenames= info["path_masks"]
		class_ids= info["class_ids"]
		nobjs= len(filenames)

		# - Read mask files
		mask = None	
		counter= 0

		for filename in filenames:
			data, header= utils.read_fits(filename,stretch=False,normalize=False,convertToRGB=False)
			height= data.shape[0]
			width= data.shape[1]
			data= data.astype(np.bool)
			if not mask:
				mask = np.zeros([height,width,nobjs],dtype=np.bool)
			mask[:,:,counter]= data
			counter+= 1

		instance_counts= np.full([mask.shape[-1]], class_ids, dtype=np.int32)
		
		# - Return mask, and array of class IDs of each instance
		return mask, instance_counts


	# ================================================================
	# ==   LOAD IMAGE
	# ================================================================
	def load_image(self, image_id):
		"""Load the specified image and return a [H,W,3] Numpy array."""
		
		# - Load image
		#logger.info("self.convert_to_rgb=%d" % self.convert_to_rgb)
		filename= self.image_info[image_id]['path']
		image, header= utils.read_fits(filename,stretch=True,normalize=True,convertToRGB=self.convert_to_rgb)
		#image, header= utils.read_fits(filename,stretch=True,normalize=True,convertToRGB=True)
				
		return image

	# ================================================================
	# ==   GET IMAGE PATH
	# ================================================================
	def image_reference(self, image_id):
		""" Return the path of the image."""

		if info["source"] == "rg-dataset":
			return info["path"]
		else:
			super(self.__class__).image_reference(self, image_id)

	
############################################################
#             TRAIN
############################################################

def train(args,model,config):    
	"""Train the model."""
	
	# - Set options
	nepochs= args.nepochs
	nthreads= args.nthreads 
	if args.grayimg:
		convert_to_rgb= False
	else:
		convert_to_rgb= True

	# - Load training/validation dataset
	logger.info("Loading train & validation dataset ...")
	dataset_train = SourceDataset()
	dataset_train.set_class_dict(args.classdict)
	dataset_train.convert_to_rgb= convert_to_rgb

	dataset_val = SourceDataset()
	dataset_val.set_class_dict(args.classdict)
	dataset_val.convert_to_rgb= convert_to_rgb

	if args.dataloader=='datalist':
		if dataset_train.load_data_from_list(args.datalist, args.maxnimgs)<0:
			logger.error("Failed to load train dataset (see logs)...")
			return -1
		if dataset_val.load_data_from_list(args.datalist, args.maxnimgs)<0:
			logger.error("Failed to load validation dataset (see logs)...")
			return -1
	elif args.dataloader=='datalist_json':
		if dataset_train.load_data_from_json_list(args.datalist, args.maxnimgs)<0:
			logger.error("Failed to load train dataset (see logs)...")
			return -1
		if dataset_val.load_data_from_json_list(args.datalist, args.maxnimgs)<0:
			logger.error("Failed to load validation dataset (see logs)...")
			return -1
	elif args.dataloader=='datadir':
		if dataset_train.load_data_from_json_search(args.datadir, args.maxnimgs)<0:
			logger.error("Failed to load train dataset (see logs)...")
			return -1
		if dataset_val.load_data_from_json_search(args.datadir, args.maxnimgs)<0:
			logger.error("Failed to load validation dataset (see logs)...")
			return -1
	else:
		logger.error("Invalid/unknown dataloader (%s) for training!" % args.dataloader)
		return -1

	dataset_train.prepare()
	dataset_val.prepare()

	# - Image augmentation
	#   http://imgaug.readthedocs.io/en/latest/source/augmenters.html
	augmentation = iaa.SomeOf((0, 2), 
		[
			iaa.Fliplr(1.0),
			iaa.Flipud(1.0),
			iaa.OneOf([iaa.Affine(rotate=90),iaa.Affine(rotate=180),iaa.Affine(rotate=270)])
		]
	)

	# - Start train
	logger.info("Start training ...")
	model.train(dataset_train, dataset_val,	
		learning_rate=config.LEARNING_RATE,
		epochs=nepochs,
		augmentation=augmentation,
		#layers='heads',
		layers='all',
		n_worker_threads=nthreads
	)

	return 0

############################################################
#        TEST
############################################################

def test(args,model,config):
	""" Test the model on input dataset with ground truth knowledge """  

	# - Set options
	if args.grayimg:
		convert_to_rgb= False
	else:
		convert_to_rgb= True

	classid_remap_dict= {}
	if args.remap_classids:
		try:
			classid_remap_dict= json.loads(args.classid_remap_dict)
		except:
			logger.error("Failed to convert class dict string to dict!")
			return -1	

	# - Create the dataset
	dataset = SourceDataset()
	dataset.set_class_dict(args.classdict)
	dataset.convert_to_rgb= convert_to_rgb

	if args.dataloader=='datalist':
		if dataset.load_data_from_list(args.datalist, args.maxnimgs)<0:
			logger.error("Failed to load test dataset (see logs)...")
			return -1
	elif args.dataloader=='datalist_json':
		if dataset.load_data_from_json_list(args.datalist, args.maxnimgs)<0:
			logger.error("Failed to load test dataset (see logs)...")
			return -1
	elif args.dataloader=='datadir':
		if dataset.load_data_from_json_search(args.datadir, args.maxnimgs)<0:
			logger.error("Failed to load test dataset (see logs)...")
			return -1
	else:
		logger.error("Invalid/unknown dataloader (%s) for testing!" % args.dataloader)
		return -1

	dataset.prepare()

	# - Test model on dataset
	tester= ModelTester(model,config,dataset)	
	tester.score_thr= args.scoreThr
	tester.iou_thr= args.iouThr
	tester.n_max_img= args.maxnimgs
	


	tester.test()

	return 0

############################################################
#        DETECT
############################################################
def detect(args,model,config):
	""" Test the model on input dataset with ground truth knowledge """  

	# - Read image data
	if args.grayimg:
		convert_to_rgb= False
	else:
		convert_to_rgb= True		

	image_data, header= utils.read_fits(args.image,stretch=True,normalize=True,convertToRGB=convert_to_rgb)
	img_fullpath= os.path.abspath(args.image)
	img_path_base= os.path.basename(img_fullpath)
	img_path_base_noext= os.path.splitext(img_path_base)[0]
	image_id= img_path_base_noext
	
	# - Apply model 
	analyzer= Analyzer(model,config)
	analyzer.draw= True
	analyzer.write_to_json= True
	analyzer.iou_thr= args.iouThr
	analyzer.score_thr= args.scoreThr

	if analyzer.predict(image_data,image_id)<0:
		logger.error("Failed to run model prediction on image %s!" % args.image)
		return -1

	# - Get results
	bboxes_det= analyzer.bboxes
	scores_det= analyzer.scores_final	
	classid_det= analyzer.class_ids_final
	masks_det= analyzer.masks_final

	# - Return if no object was detected
	if not bboxes_det:
		logger.info("No object detected in image %s ..." % args.image)
		return 0
	
	# - Print results
	logger.info("#%d objects found in image %s ..." % (len(bboxes_det),args.image))
	print("bboxes_det")
	print(bboxes_det)
	print("scores_det")
	print(scores_det)
	print("classid_det")
	print(classid_det)
	print("masks_det")
	print(type(masks_det))
	for mask in masks_det:
		print(type(mask))
		print(mask.shape)
		print(mask)

	
	return 0

############################################################
#        PARSE/VALIDATE ARGS
############################################################

def parse_args():
	""" Parse command line arguments """  
  
	# - Parse command line arguments
	parser = argparse.ArgumentParser(description='Train Mask R-CNN to detect radio sources.')

	parser.add_argument("command",metavar="<command>",help="'train' or 'test'")

	# - COMMON OPTIONS
	parser.add_argument('--grayimg', dest='grayimg', action='store_true')	
	parser.set_defaults(grayimg=False)
	parser.add_argument('--classdict', dest='classdict', required=False, type=str, default='{"sidelobe":1,"source":2,"galaxy":3}',help='Class id dictionary used when loading dataset') 
	parser.add_argument('--classdict_model', dest='classdict_model', required=False, type=str, default='',help='Class id dictionary used for the model (if empty, it is set equal to classdict)')
	parser.add_argument('--remap_classids', dest='remap_classids', action='store_true')	
	parser.set_defaults(remap_classids=False)
	parser.add_argument('--classid_remap_dict', dest='classid_remap_dict', required=False, type=str, default='',help='Dictionary used to remap detected classid to gt classid')
 
	parser.add_argument('--dataloader',required=False,metavar="Data loader type",type=str,default='filelist',help='Train/test data loader type {datalist,datalist_json,datadir_json}')
	parser.add_argument('--datalist', required=False,metavar="/path/to/dataset",help='Train/test data filelist with format: filename_img,filename_mask,label or: filename_json')
	parser.add_argument('--datadir', required=False,metavar="/path/to/dataset",help='Train/test data top dir traversed to search json dataset files')
	parser.add_argument('--maxnimgs', required=False,metavar="",type=int,default=-1,help="Max number of images to consider in dataset (-1=all) (default=-1)")
	parser.add_argument('--weights', required=False,metavar="/path/to/weights.h5",help="Path to weights .h5 file")
	parser.add_argument('--logs', required=False,default=DEFAULT_LOGS_DIR,metavar="/path/to/logs/",help='Logs and checkpoints directory (default=logs/)')
	parser.add_argument('--nthreads', required=False,default=1,type=int,metavar="Number of worker threads",help="Number of worker threads")

	# - TRAIN OPTIONS
	parser.add_argument('--ngpu', required=False,default=1,type=int,metavar="Number of GPUs",help='Number of GPUs')
	parser.add_argument('--nimg_per_gpu', required=False,default=1,type=int,metavar="Number of images per gpu",help='Number of images per gpu')
	parser.add_argument('--nepochs', required=False,default=10,type=int,metavar="Number of training epochs",help='Number of training epochs')
	parser.add_argument('--epoch_length', required=False,type=int,default=1,metavar="Number of data batches per epoch",help='Number of data batches per epoch, usually equal to train sample size.')
	parser.add_argument('--nvalidation_steps', required=False,default=1,type=int,metavar="Number of validation steps per epoch",help='Number of validation steps per epoch. Default is 0.')
	parser.add_argument('--rpn_anchor_scales', dest='rpn_anchor_scales', required=False, type=str, default='2,4,8,16,32,64',help='RPN anchor scales') 
	parser.add_argument('--max_gt_instances', dest='max_gt_instances', required=False, type=int, default=300,help='Max GT instances') 
	parser.add_argument('--backbone', dest='backbone', required=False, type=str, default='resnet101',help='Backbone network {resnet101,resnet50,custom} (default=resnet101)') 
	parser.add_argument('--backbone_strides', dest='backbone_strides', required=False, type=str, default='2,4,8,16,32,64',help='Backbone strides') 
	parser.add_argument('--rpn_nms_threshold', dest='rpn_nms_threshold', required=False, type=float, default=0.7,help='RPN Non-Maximum-Suppression threshold (default=0.7)') 
	parser.add_argument('--rpn_train_anchors_per_image', dest='rpn_train_anchors_per_image', required=False, type=int, default=512,help='Number of anchors per image to use for RPN training (default=512)')
	parser.add_argument('--train_rois_per_image', dest='train_rois_per_image', required=False, type=int, default=512,help='Number of ROIs per image to feed to classifier/mask heads (default=512)')
	parser.add_argument('--rpn_anchor_ratios', dest='rpn_anchor_ratios', required=False, type=str, default='0.5,1,2',help='RPN anchor ratios') 
	

	# - TEST OPTIONS
	parser.add_argument('--scoreThr', required=False,default=0.7,type=float,metavar="Object detection score threshold to be used during test",help="Object detection score threshold to be used during test")
	parser.add_argument('--iouThr', required=False,default=0.6,type=float,metavar="IOU threshold used to match detected objects with true objects",help="IOU threshold used to match detected objects with true objects")

	# - DETECT OPTIONS
	parser.add_argument('--image',required=False,metavar="Input image",type=str,help='Input image in FITS format to apply the model (used in detect task)')

	args = parser.parse_args()

	#return vars(args)
	return args


def validate_args(args):
	""" Validate arguments """
	
	# - Check commands
	if args.command != "train" and args.command != "test" and args.command != "detect":
		logger.error("Unknow command (%s) given, only train/test/detect supported!" % args.command)
		return -1

	# - Check data loaders
	if args.dataloader=='datalist' or args.dataloader=='datalist_json':
		has_datalist= (args.datalist and args.datalist!="")
		if not has_datalist:
			logger.error("Argument --datalist is required for training with datalist data loader!")
			return -1
	elif args.dataloader=='datadir_json':
		has_datadir= (args.datadir and args.datadir!="")
		dir_exist= os.path.isdir(args.datadir)
		if not has_datadir:
			logger.error("Argument --datadir is required for training with datadir data loader!")
			return -1
		if not dir_exist:
			logger.error("Datadir argument must be a directory existing on filesystem!")
			return -1

	# - Check image arg
	if args.command=='detect':
		has_image= (args.image and args.image!="")
		image_exists= os.path.isfile(args.image)
		valid_extension= args.image.endswith('.fits')
		if not has_image:
			logger.error("Argument --image is required for detect task!")
			return -1
		if not image_exists:
			logger.error("Image argument must be an existing image on filesystem!")
			return -1
		if not valid_extension:
			logger.error("Image must have .fits extension!")
			return -1

	# - Check maxnimgs
	if args.maxnimgs==0 or (args.maxnimgs<0 and args.maxnimgs!=-1):
		logger.error("Invalid maxnimgs given (hint: give -1 or >0)!")
		return -1

	# - Check weight file exists
	# ...

	# - Check remap id
	if args.remap_classids:
		if args.classid_remap_dict=="":
			logger.error("Classid remap dictionary is empty (you need to provide one if you give the option --remap_classids)!")
			return -1

	return 0

############################################################
#       MAIN
############################################################
def main():
	"""Main function"""

	#===========================
	#==   PARSE ARGS
	#===========================
	logger.info("Parsing script args ...")
	try:
		args= parse_args()
	except Exception as ex:
		logger.error("Failed to get and parse options (err=%s)",str(ex))
		return 1

	#===========================
	#==   VALIDATE ARGS
	#===========================
	logger.info("Validating script args ...")
	if validate_args(args)<0:
		logger.error("Argument validation failed, exit...")
		return 1

	print("Weights: ", args.weights)
	print("Datalist: ", args.datalist)
	print("Logs: ", args.logs)
	print("nEpochs: ",args.nepochs)
	print("epoch_length: ",args.epoch_length)
	print("nvalidation_steps: ",args.nvalidation_steps)
	print("ngpu: ",args.ngpu)
	print("nimg_per_gpu: ",args.nimg_per_gpu)
	print("scoreThr: ",args.scoreThr)
	print("classdict: ",args.classdict)

	#===========================
	#==   SET PARAMETERS
	#===========================
	weights_path = args.weights

	rpn_ancor_scales= tuple([int(x.strip()) for x in args.rpn_anchor_scales.split(',')])
	backbone_strides= [int(x.strip()) for x in args.backbone_strides.split(',')]
	rpn_anchor_ratios= [float(x.strip()) for x in args.rpn_anchor_ratios.split(',')]

	train_from_scratch= False
	if not weights_path or weights_path=='':
		train_from_scratch= True

	try:
		class_dict= json.loads(args.classdict)
	except:
		logger.error("Failed to convert class dict string to dict!")
		return -1	

	class_dict_model= class_dict
	if args.classdict_model!="":
		try:
			class_dict_model= json.loads(args.classdict_model)
		except:
			logger.error("Failed to convert class dict model string to dict!")
			return -1		

	nclasses= len(class_dict)
	nclasses_model= len(class_dict_model)
	
	class_names= ["bkg"]
	for class_name in class_dict:
		class_names.append(class_name)
	
	class_names_model= ["bkg"]
	for class_name in class_dict_model:
		class_names_model.append(class_name)
	
	logger.info("Assuming #%d+1 classes in dataset from given class dictionary ..." % nclasses)
	print("CLASS_NAMES (DATASET)")
	print(class_names)
	
	logger.info("Assuming #%d+1 classes in model from given class dictionary ..." % nclasses_model)
	print("CLASS_NAMES (MODEL)")
	print(class_names_model)

	
	steps_per_epoch= ((args.epoch_length - args.nvalidation_steps) // (args.nimg_per_gpu*args.ngpu))
	validation_steps_per_epoch= max(1, args.nvalidation_steps // (args.nimg_per_gpu*args.ngpu))
	
	#===========================
	#==   CONFIG
	#===========================
	if args.command == "train":
		config = SDetectorConfig()
		config.NUM_CLASSES = nclasses_model + 1
		config.CLASS_NAMES = class_names_model
		config.IMAGE_META_SIZE = 1 + 3 + 3 + 4 + 1 + config.NUM_CLASSES
		config.GPU_COUNT = args.ngpu
		config.IMAGES_PER_GPU = args.nimg_per_gpu
		#config.VALIDATION_STEPS = max(1, args.nvalidation_steps // (config.IMAGES_PER_GPU*config.GPU_COUNT)) # 200 validation/test images
		#config.STEPS_PER_EPOCH = ((args.epoch_length - args.nvalidation_steps) // (config.IMAGES_PER_GPU*config.GPU_COUNT)) #16439 total images
		config.VALIDATION_STEPS= validation_steps_per_epoch
		config.STEPS_PER_EPOCH= steps_per_epoch
		
	elif args.command == "test":
		class InferenceConfig(SDetectorConfig):
			# Set batch size to 1 since we'll be running inference on
			# one image at a time. Batch size = GPU_COUNT * IMAGES_PER_GPU
			GPU_COUNT = 1 # don't use GPU should be =0 but not working
			IMAGES_PER_GPU = 1 # don't use GPU should be =0 but not working
		config = InferenceConfig()
		config.NUM_CLASSES = nclasses_model + 1
		config.CLASS_NAMES = class_names_model
		config.IMAGE_META_SIZE = 1 + 3 + 3 + 4 + 1 + config.NUM_CLASSES

	elif args.command == "detect":
		class InferenceConfig(SDetectorConfig):
			# Set batch size to 1 since we'll be running inference on
			# one image at a time. Batch size = GPU_COUNT * IMAGES_PER_GPU
			GPU_COUNT = 1 # don't use GPU should be =0 but not working
			IMAGES_PER_GPU = 1 # don't use GPU should be =0 but not working
		config = InferenceConfig()
		config.NUM_CLASSES = nclasses_model + 1
		config.CLASS_NAMES = class_names_model
		config.IMAGE_META_SIZE = 1 + 3 + 3 + 4 + 1 + config.NUM_CLASSES

	# - Override some other options		
	config.RPN_ANCHOR_SCALES= rpn_ancor_scales
	config.MAX_GT_INSTANCES= args.max_gt_instances
	config.BACKBONE= args.backbone
	config.BACKBONE_STRIDES= backbone_strides
	config.RPN_NMS_THRESHOLD= args.rpn_nms_threshold
	config.RPN_TRAIN_ANCHORS_PER_IMAGE= args.rpn_train_anchors_per_image
	config.TRAIN_ROIS_PER_IMAGE= args.train_rois_per_image
	config.RPN_ANCHOR_RATIOS= rpn_anchor_ratios

	config.display()

	#===========================
	#==   CREATE MODEL
	#===========================
	# - Create model
	if args.command == "train":
		model = modellib.MaskRCNN(mode="training", config=config,model_dir=args.logs)
	elif args.command == "test" or args.command == "detect":
		# Device to load the neural network on.
		# Useful if you're training a model on the same 
		# machine, in which case use CPU and leave the
		# GPU for training.
		DEVICE = "/cpu:0"  # /cpu:0 or /gpu:0
		with tf.device(DEVICE):
			model = modellib.MaskRCNN(mode="inference", config=config,model_dir=args.logs)

	logger.info("Printing the model ...")
	model.print_model()

	# - Load weights
	if train_from_scratch:
		logger.info("No weights given, training from scratch ...")
	else:	
		logger.info("Loading weights from file %s ..." % weights_path)
		model.load_weights(weights_path, by_name=True)
	
	#===========================
	#==   TRAIN/TEST
	#===========================
	# - Train or evaluate
	if args.command == "train":
		if train(args,model,config)<0:
			logger.error("Failed to run train!")
			return 1
	elif args.command == "test":
		if test(args,model,config)<0:
			logger.error("Failed to run test!")
			return 1
	elif args.command == "detect":
		if detect(args,model,config)<0:
			logger.error("Failed to run detect!")
			return 1		
	
	return 0


###################
##   MAIN EXEC   ##
###################
if __name__ == "__main__":
	sys.exit(main())

