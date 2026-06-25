#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to copy reflection model to share drive
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import os
import glob
import shutil
import argparse
import Utils
# --------

# File version
__version__ = "2.1.0"

# argument definition
parser = argparse.ArgumentParser()
parser.add_argument("pathOfEaModelToCopy", help="Path of the model to copy.")
parser.add_argument("targetName", help="Name of the reflection model.")
parser.add_argument("targetPath", help="Path of the reflection model.")
args = parser.parse_args()

pathOfEaModelToCopy = args.pathOfEaModelToCopy
targetName = args.targetName
targetPath = args.targetPath
# ---------------------
print(f"== Reflection Model Copy Script v{__version__} ==")
os.chdir(pathOfEaModelToCopy)

# Currently, the first found .eapx/.qea file is considered
model = Utils.GetFilesInFolder(pathOfEaModelToCopy)

print(model)

for filename in model:
    shutil.copy(filename, f"{targetPath}/")
