#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation of utility functions for MPMS methodology
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import os
import re
import subprocess
import json
import glob
from git import Repo
import shutil
import win32com
from winreg import *
from pathlib import Path
import xml.etree.ElementTree
from ifxPyArch.utilities.EAUtils.EAUtils import EAWrapper as EA, EASessionError, EADBTypes
from ifxPyArch.utilities.eden_lib import ea_automation_extension as EALib
# --------

# File version
__version__ = "2.6.0"


def get_emptymodel(folder, extensionFilter=['qeax','eapx']):
    if type(extensionFilter) == list:
        for extension in extensionFilter:
            fileFilter = f"{folder}/*.{extension}"
            fileList = glob.glob(fileFilter)
            if fileList:
                return fileList[0]
# --------
lemonTreeTool = r"C:/Program Files/LieberLieber/LemonTree.Automation/LemonTree.Automation.exe"
here = Path(__file__).parent
mappingFilePath = r"C:/git-repos/aurix_rc1_sw_mcal/06_Environment/Model_env/01_ProjectConfigs/ComponentPathMappings.json"
emptyModel = get_emptymodel(r"C:/git-repos/aurix_rc1_sw_mcal/06_Environment/Model_env/00_BaseTemplate")
# --------


def LoadComponentMappings():
    mappingFile = open(mappingFilePath, encoding="utf8")
    componentMappings = json.load(mappingFile)
    mappingFile.close()
    return componentMappings


def DownloadFileFromBitBucket(directory, filename, username, password, branch, tabsize=0):
    componentMappings = LoadComponentMappings()
    bitBucketBaseURL = componentMappings["BitBucketBaseURL"]

    bitBucketRepoName = None
    mapping = GetMappingByName(componentMappings, filename)

    if mapping is not None:
        filename = mapping["Name"]
        bitBucketRepoName = mapping["BitBucketRepoName"]
        url = (
            bitBucketBaseURL
            + "/"
            + bitBucketRepoName
            + "/raw/"
            + filename.replace(" ", "%20").replace("\\", "/")
            + "?at=refs%2Fheads%2F"
            + branch.replace("/", "%2F")
        )
        print(("\t" * (tabsize)) + f"URL for download: {url}")

    isValidFile = False

    if bitBucketRepoName is not None:
        filename = os.path.basename(filename)
        print(("\t" * (tabsize)) + f"Downloading file {filename}...")

        # example url:
        # https://bitbucket.vih.infineon.com/projects/ATVMPMS/repos/mpms_sw_prj_dev_comm_dev/raw/00_Architecture/Common-1e6c8f00-295c-572e-e160-0d362920588e.mpms?at=refs%2Fheads%2Fdevelop
        subprocess.call(["curl.exe", "-u", f"{username}:{password}", url, "-o", filename, "-s"])

        mappingFile = open(mappingFilePath, encoding="utf8")
        componentMappings = json.load(mappingFile)
        mappingFile.close()

        mpmsFile = open(f"{directory}/{filename}", encoding="utf8")
        mpmsFileContent = mpmsFile.read()
        mpmsFile.close()

        if not (
            "<title>Permission denied - Bitbucket</title>" in mpmsFileContent or
            ("does not exist at revision" in mpmsFileContent and
             "<title>Oops, can&#39;t find that - Bitbucket</title>" in mpmsFileContent)
        ):
            isValidFile = True

        if not isValidFile:
            os.remove(filename)
    else:
        print(("\t" * (tabsize)) + f"The file {filename} doesn't exist on BitBucket.")

    return isValidFile


def LemonTreeImportMpmsFiles(fullImportPath, mpmsPath):
    print(f"FullImportPath ::: {fullImportPath}")
    print(f"MPMS path :: {mpmsPath}")
    try:
        p = subprocess.run(
            [
                lemonTreeTool,
                "import",
                "--model",
                fullImportPath,
                "--Components",
                mpmsPath,
            ],
            check=True,
        )

        return p.returncode
    except Exception as err:
        raise Exception(f"Error: LemonTree execution code failed due to -- {err}")


def LemonTreePublishComponent(exportModel, exportPath, componentGuids):
    p = subprocess.run(
        [
            lemonTreeTool,
            "publish",
            "--model",
            exportModel,
            "--packagedirectory",
            exportPath,
            "--componentguids",
            componentGuids,
        ],
        check=True,
    )

    return p.returncode


def LemonTreeGenerateDiffSession(base, theirs, mine, diffSessionOutputPath):
    p = subprocess.run(
        [
            lemonTreeTool,
            "diff",
            "--base",
            base,
            "--theirs",
            theirs,
            "--mine",
            mine,
            "--sfs",
            diffSessionOutputPath,
        ],
        check=True,
    )

    return p.returncode


def LemonTreeGenerateMergeSession(base, theirs, mine, mergeSessionOutputPath):
    p = subprocess.run(
        [
            lemonTreeTool,
            "merge",
            "--DryRun",
            "--base",
            base,
            "--theirs",
            theirs,
            "--mine",
            mine,
            "--sfs",
            mergeSessionOutputPath,
        ],
        check=True,
    )

    return p.returncode


def LemonTreePublishComponents(model, componentGuids, path):
    p = subprocess.run(
        [
            lemonTreeTool,
            "publish",
            "--model",
            model,
            "--packagedirectory",
            path,
            "--componentGuids",
            componentGuids,
        ],
        check=True,
    )

    return p.returncode


def GetFilesInFolder(folder, extensionFilter=['qeax', 'eapx']):
    if type(extensionFilter) == list:
        for extension in extensionFilter:
            fileFilter = f"{folder}/*.{extension}"
            fileList = glob.glob(fileFilter)
            if fileList:
                return fileList
    else:
        fileFilter = f"{folder}/{extensionFilter}"
        fileList = glob.glob(fileFilter)
        return fileList


def GetFirstFileInFolder(folder, extensionfilter=['qeax', 'eapx']):
    fileList = GetFilesInFolder(folder, extensionfilter)
    if fileList:
        return fileList[0]
    else:
        return None


def GetMappingByName(componentMappings, name):
    for m in componentMappings["Components"]:
        if os.path.basename(m["Name"]) == os.path.basename(name):
            return m


def GetGitStatus(repo, branch):
    result = ""
    repo.remotes.origin.fetch()
    commits_behind = repo.iter_commits(f"{branch}..origin/{branch}")
    count_commits_behind = sum(1 for c in commits_behind)
    commits_ahead = repo.iter_commits(f"origin/{branch}..{branch}")
    count_commits_ahead = sum(1 for c in commits_ahead)
    changedFiles = [item.a_path for item in repo.index.diff(None)]

    result += f"\t\tCommits behind  : {count_commits_behind}\n"
    result += f"\t\tCommits ahead   : {count_commits_ahead}\n"
    result += f"\t\tLocal changes   : {changedFiles}"

    return [count_commits_behind, count_commits_ahead, result]


def GitFixDetachedHead():
    subprocess.call("git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'")


def RunGitCommandWithResult(command):
    result = subprocess.check_output(command, shell=True).strip()
    # convert to string, since the result of the git command is not string
    return str(result, "UTF-8")


def CreateMpmsWorkspace(directory):
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    os.makedirs(directory)


def ClearReadOnlyFlag(mpmsfile):
    # manipulate the read only flag
    file = open(mpmsfile, "r", encoding="utf8")
    mpmsFileContent = file.read()
    file.close()

    mpmsFileContent = mpmsFileContent.replace('//{"ReadOnly":true', '//{"ReadOnly":false')
    file = open(mpmsfile, "w", encoding="utf8")
    file.write(mpmsFileContent)
    file.close()


def LockPackagesRecursively(eapFile):
    print(f"Locking all packages in Reflection Model '{eapFile}'")
    try:
        EA.OpenFileNGetComponents(eapFile)
        query = "select * from t_package where t_package.Parent_ID = 0"
        result = EA.EASession.SQLQuery(query)
        root = xml.etree.ElementTree.fromstring(result)
        pkgIDList = [node.find("Package_ID").text for node in root.findall(".//Dataset_0//Data//Row")]
    except Exception as err:
        raise RuntimeError("Error fetching root packages from EA.Repository").with_traceback(err.__traceback__)
    else:
        try:
            for pkgID in pkgIDList:
                pkg = EA.EASession.GetPackageByID(pkgID)
                pkg.SetReadOnly(ReadOnly=True, IncludeSubPkgs=True)
                pkg.Update()
        except Exception as err:
            raise RuntimeError(f"Failed: Package {pkg.Name} could not be locked. Model may be corrupted!").with_traceback(err.__traceback__)
    finally:
        print("Packages locked successfully!")
        EA.ExitSession()


def LockNCompactReflectionModel(eapFile, mpmsworkspace):
    eapFilePath = os.path.dirname(os.path.abspath(eapFile))
    eapFileName = os.path.basename(eapFile)

    # Open EA instance
    try:
        eaSession = EALib.IRepository(win32com.client.Dispatch("EA.Repository"))
    except Exception as err:
        raise RuntimeError("Error opening EA.Repository").with_traceback(err.__traceback__)

    #  Transfer EA model to new EA project
    try:
        print(f"Repairing and compacting Reflection Model '{eapFile}'")
        os.rename(eapFile, f"{eapFilePath}\\Cloned_{eapFileName}")
        src  = f"{eapFilePath}\\Cloned_{eapFileName}"

        pi = eaSession.GetProjectInterface()
        pi.ProjectTransfer(src, eapFile, f'{mpmsworkspace}\\repairLog.txt')
    except Exception as err:
        raise RuntimeError(f"Error repairing {eapFile}!").with_traceback(err.__traceback__)
    else:
        print("Reflection model compacted and repaired successfully!")
    finally:
        eaSession.Exit()
        os.remove(src)

def SortLegendByPRMT(xrefDesc):
    sortPattern = re.compile(r'(@PRMT=(\d+|@))')
    legends = list(filter(None, re.split('@ENDPROP;', xrefDesc)))
    legendsSortedByPRMT = sorted(legends, key=lambda o: sortPattern.search(o).group(1))
    updatedXrefDesc = f'{"@ENDPROP;".join(legendsSortedByPRMT)}@ENDPROP;'
    return updatedXrefDesc

def FixDiagramLegendProps(eapFile):
    print(f"[Workaround] Fixing Legends overlap in '{eapFile}'")
    try:
        EA.OpenFileNGetComponents(eapFile)
        pattern = r"%Legend_Type%" if EA._EaDbType == EADBTypes.Server or EA._EaDbType == EADBTypes.SQLLite else r"*Legend_Type*"
        query = f'select o.XrefID, o.Description from t_xref o where o.Description LIKE "{pattern}"'
        qresult = EA.EASession.SQLQuery(query)
        root = xml.etree.ElementTree.fromstring(qresult)
        for node in root.findall(".//Dataset_0//Data//Row"):
            xrefID = node.find("XrefID").text
            xrefDesc = node.find("Description").text
            updatedXrefDesc = SortLegendByPRMT(xrefDesc)
            if updatedXrefDesc != xrefDesc:
                query = f'update t_xref set Description="{updatedXrefDesc}" where XrefID="{xrefID}"'
                EA.EASession.Execute(query)
    except EASessionError as err:
        raise RuntimeError(err.args[0]).with_traceback(err.__traceback__)
    except Exception as err:
        raise RuntimeError("Error updating Legends props").with_traceback(err.__traceback__)
    finally:
        print("[Workaround] Legends overlap fixed!")
        EA.ExitSession()


def get_dependency_mpms(mpmsdata):
    jsonContent = json.loads(mpmsdata)
    count = 0
    componentFileName = ""
    print("Dependent component(s) ::")
    dependency_list = []
    for dependency in jsonContent["Dependencies"]:
        if count != 0:
            componentFileName += ","
        count += 1
        name = dependency["name"]
        targetFull = dependency["target"]
        target = targetFull[1: len(targetFull) - 1]
        filename = f"{name}-{target}.mpms"
        dependency_list.append(filename)
    return dependency_list


def check_local_repostatus(componentmappings, gitbasePath, filename):
    # get local files
    localPath = ""
    gitStatus = None
    componentMapping = GetMappingByName(componentmappings, filename)

    if componentMapping is not None:
        localPath = componentMapping["LocalPath"]
        localFileName = componentMapping["Name"]
        try:
            # Check local repository status
            gitpath = f"{gitbasePath}/{localPath}"
            os.chdir(gitpath)
            repo = Repo(gitpath)
            assert not repo.bare
            branch = repo.active_branch
            gitBranch = branch.name
            gitStatus = GetGitStatus(repo, branch)
        except FileNotFoundError as ex:
            print(f"\t - File NOT FOUND in local repo! Placeholders will be imported.")
        else:
            print(
                f"\t - Imported from Branch '{gitBranch}'. Git status compared to remote:\n{gitStatus[-1]}\n")
        return [gitStatus, gitBranch, localPath, localFileName]
    else:
        print(
            f"\t - File DOESN'T EXIST in ComponentPathMappings.json! Placeholders will be imported.")
            
def GetTargetBranchFromPR(mpmsfile, username, password, branch_name):
    """
    Fetch the target branch by querying open PRs.

    Args:
        mpmsfile (str): The MPMS file name.
        username (str): Bitbucket username.
        password (str): Bitbucket app password.
        branch_name (str): The branch name to query.

    Returns:
        str: Target branch name if found, otherwise None.
    """
    # Load ComponentPathMappings.json
    componentMappings = LoadComponentMappings()

    # Extract BitBucketRepoName for the MPMS file
    mapping = GetMappingByName(componentMappings, mpmsfile)
    if not mapping:
        print(f"No mapping found for MPMS file: {mpmsfile}")
        return "None"

    # Construct API URL
    repo_slug = mapping["BitBucketRepoName"]
    base_url = f"{componentMappings['BitBucketBaseURL']}/{repo_slug}/rest/api/1.0"
    pr_url = f"{base_url}/pull-requests"
    
    # Fetch open pull requests
    try:
        pr_response = subprocess.check_output([
            "curl",
            "-u", f"{username}:{password}",
            pr_url,
            "-G",
            "--data-urlencode", "state=OPEN"  # Only open pull requests
        ]).decode("utf-8")

        pr_data = json.loads(pr_response)

        # Parse PR data
        if "values" not in pr_data:
            print(f"No open PRs found in repository: {repo_slug}")
            return "None"

        for pr in pr_data.get("values", []):
            from_ref = pr.get("fromRef", {}).get("displayId")
            to_ref = pr.get("toRef", {}).get("displayId")

            if from_ref and branch_name in from_ref:
                print(f"Open PR found for branch '{branch_name}'. Target branch: '{to_ref}'")
                return to_ref

        print(f"No open PRs found for branch: {branch_name}")
        return "None"

    except subprocess.CalledProcessError as e:
        print(f"Error while fetching pull requests: {e}")
        return "None"