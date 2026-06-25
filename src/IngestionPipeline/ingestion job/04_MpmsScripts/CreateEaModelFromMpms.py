#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to create EA Model from MPMS file (incl its dependencies)
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import argparse
import sys
import os
import shutil
import getpass
import Utils
from pathlib import Path
import glob

# --------
# File version
__version__ = "2.4.0"

# argument definition
parser = argparse.ArgumentParser(description="Create EA Model Script")
parser.add_argument("-f", "--pathToMpmsFile", help="Path of the repository where the model is created.", required=True)
parser.add_argument("-b", "--branch", help="Git branch.", default="master", required=False)
parser.add_argument(
    "--jenkins",
    action="store_true",
    help="If set to true, any existing ea model will be overwritten. Local users should not pass this variable",
    required=False,
)
parser.add_argument(
    "--getFilesFromBitBucket",
    action="store_true",
    help="If set to true, files we be download from BitBucket. If set to false, local files we be used.",
    required=False,
)
parser.add_argument("--username", default="", required=False)
parser.add_argument("--password", default="", required=False)
parser.add_argument("-t", "--targetBranchName", help="Specify the target branch for the EAPX/QEAX Naming.", required=True)

args = parser.parse_args()

pathToMpmsFile = args.pathToMpmsFile
getFilesFromBitBucket = args.getFilesFromBitBucket
jenkins = args.jenkins
username = args.username
password = args.password
branch = args.branch
target_branch_name  = args.targetBranchName

# ---------------------
print(f"== Create Model from MPMS v{__version__} ==")
_importLog = {}
mpmsworkspace = r"C:/temp/mpms-workspace"
print(f"Creating temporary workspace for scripts: '{mpmsworkspace}'")
Utils.CreateMpmsWorkspace(mpmsworkspace)

mpmsfile = Utils.GetFirstFileInFolder(pathToMpmsFile, "*.mpms")
eaFileExtn = Path(Utils.emptyModel).suffix


if target_branch_name == "None":
    # Setup credentials to be used to fetch PR details from BitBucket
    if not jenkins:
        usr = getpass.getuser()
        pwd = getpass.getpass(prompt="Credential required to download file from BitBucket. Enter password: ")
    else:
        usr = username
        pwd = password
    target_branch_name  = Utils.GetTargetBranchFromPR(mpmsfile, usr, pwd, branch)
    newModelName = f"{Path(mpmsfile).parent}\{target_branch_name}_rc1_sw_mcal{eaFileExtn}"
else:
    newModelName = f"{Path(mpmsfile).parent}\{target_branch_name}_rc1_sw_mcal{eaFileExtn}"

# Get User credential, if files to be downloaded from BitBucket
if getFilesFromBitBucket:
    # Setup credentials to be used to download file from BitBucket
    if not jenkins:
        usr = getpass.getuser()
        pwd = getpass.getpass(prompt="Credential required to download file from BitBucket. Enter password: ")
    else:
        usr = username
        pwd = password

# Check if any older model is already present, if yes backup the model
if not jenkins:
    if os.path.exists(newModelName):
        if not os.path.isdir("temp"):
            os.makedirs("temp")
        print(f"Found model exists already! Copying existing model to {os.getcwd()}\\temp")
        shutil.copy(newModelName, "temp")

# Copy base EAP/QEA to current directory
if newModelName is not None:
    shutil.copy(Utils.emptyModel, newModelName)

# Copy the mpms files to mpms workspace
fileList = Utils.GetFilesInFolder(pathToMpmsFile, "*.mpms")
for file in fileList:
    shutil.copy(file, mpmsworkspace)

# Process each file and fetch all its dependencies
componentMappings = Utils.LoadComponentMappings()
gitBasePath = componentMappings["GitRepoBasePath"]
for file in Utils.GetFilesInFolder(mpmsworkspace, "*.mpms"):
    print(f"Importing              :: {os.path.basename(file)}")
    # Clear the read-only flag of main mpms files
    Utils.ClearReadOnlyFlag(file)

    # Copy the dependent mpms files to mpms workspace
    rfile = open(file, encoding="utf8")
    lines = rfile.readlines()
    for line in lines:
        if line.startswith("//{"):
            substring = line[2 : len(line) - 1]
            dependency_list = Utils.get_dependency_mpms(substring)
            for filename in dependency_list:
                # download all files from BitBucket
                if getFilesFromBitBucket:
                    # Setup credentials to be used to download file from BitBucket
                    if not jenkins:
                        usr = getpass.getuser()
                        pwd = getpass.getpass(prompt="Credential required to download file from BitBucket. Enter password: ")
                    else:
                        usr = username
                        pwd = password

                    os.chdir(mpmsworkspace)  # change to mpms workspace as curl would download to current directory
                    # download from BitBucket
                    isSuccess = Utils.DownloadFileFromBitBucket(mpmsworkspace, filename, usr, pwd, branch, tabsize=1)
                    if not isSuccess:
                        print(f"\t[Error] File {filename} could not be downloaded or DOESN'T EXIST in the branch '{branch}'! Placeholders will be imported.")
                    else:
                        print(f"\t[Info] Imported from BitBucket. Branch: '{branch}'")
                    os.chdir(pathToMpmsFile)  # switch back to user provided directory
                else:
                    print(f" + {filename}")
                    localpath = Utils.check_local_repostatus(componentMappings, gitBasePath, filename)
                    if localpath:
                        # Copy the dependent mpms file to mpms workspace
                        shutil.copy(f"{gitBasePath}/{localpath[2]}/{localpath[3]}", mpmsworkspace)
    rfile.close()

# Import mpms files to EAP/QEA
print("\n=== Detailed log ===")
returncode = Utils.LemonTreeImportMpmsFiles(newModelName, f"{mpmsworkspace}/*.mpms")
if returncode != 0:
    print(f"Import was not successful, Exit Code: {returncode}")
else:
    # Update the legend props to avoid overlap
    try:
        Utils.FixDiagramLegendProps(newModelName)
    except Exception as err:
        print(err)

# Clean the workspace before exit
Utils.CreateMpmsWorkspace(mpmsworkspace)
sys.exit(returncode)
