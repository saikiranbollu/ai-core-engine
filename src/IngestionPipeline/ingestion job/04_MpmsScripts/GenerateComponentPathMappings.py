#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to generate component mapping JSON file
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import os
import sys
import json
import argparse
from typing import List
from dataclasses import dataclass

# File version
__version__ = "2.0.0"

def mpmsSerializer(obj):
    if hasattr(obj, "to_json"):
        return obj.to_json()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


@dataclass
class Component:
    def __init__(self, fileName) -> None:
        self.Name: str = fileName
        self.LocalPath: str = None
        self.BitBucketRepoName: str = None

        # Get module name from file name. [Note: Special case for common, which shall be treated as comm]
        moduleName = fileName.split(".")[0]
        print(f"Generating map :: {fileName}")
        if moduleName == "Common":
            moduleName = "Comm"
            modRoot = rf"03_Development\MCAL\00_Common"
        else:
            modRoot = rf"03_Development\MCAL\{moduleName}"

        if "Architecture" in fileName:
            self.LocalPath = rf"{modRoot}\docs\00_Architecture"
            self.BitBucketRepoName = rf"aurix_rc1_sw_mcal_dev_{moduleName}_arch".lower()
        elif "DetailedSpecification" in fileName:
            self.LocalPath = rf"{modRoot}\docs\01_Design"
            self.BitBucketRepoName = rf"aurix_rc1_sw_mcal_dev_{moduleName}_design".lower()
        elif "UserManual" in fileName:
            self.LocalPath = rf"{modRoot}\docs\02_UserManual"
            self.BitBucketRepoName = rf"aurix_rc1_sw_mcal_dev_{moduleName}_um".lower()
        else:
            print(f"Unsupported File: {fileName}")

    def __iter__(self):
        yield from {
            "Name": self.Name,
            "LocalPath": self.LocalPath,
            "BitBucketRepoName": self.BitBucketRepoName,
        }.items()

    def __str__(self):
        return json.dumps(dict(self), ensure_ascii=False)

    def __repr__(self):
        return self.__str__()

    def to_json(self):
        return self.__str__()


@dataclass
class ComponentPathMappings:
    def __init__(self, srcDir, bbUrl, basePath) -> None:
        self.BitBucketBaseURL: str = bbUrl
        self.GitRepoBasePath: str = basePath

        # Populate the components
        self.Components: List[Component] = []
        files_path = [os.path.basename(x) for x in os.listdir(srcDir)]
        for file in files_path:
            if os.path.splitext(file)[1] == ".mpms":
                self.Components.append(Component(fileName=file))

    def __iter__(self):
        yield from {
            "BitBucketBaseURL": self.BitBucketBaseURL,
            "GitRepoBasePath": self.GitRepoBasePath,
            "components": self.Components,
        }.items()

    def __str__(self):
        return json.dumps(self.to_json())

    def __repr__(self):
        return self.__str__()

    def to_json(self):
        to_return = {"BitBucketBaseURL": self.BitBucketBaseURL, "GitRepoBasePath": self.GitRepoBasePath}
        components = []
        for o in self.Components:
            components.append(o.__dict__)
        to_return["components"] = components
        return to_return


def main(params):
    """
    Generate the ComponentPathMappings.json file
    """
    bbUrl = params.bb_url
    basePath = params.local_repo
    jsonFile = f"{params.outputdir}\\ComponentPathMappings.json"
    root = ComponentPathMappings(srcDir=params.source, bbUrl=bbUrl, basePath=basePath)

    # Print to JSON file
    try:
        print(f"== ComponentPathMappings Generator v{__version__} ==")
        json_string = json.dumps(root, default=mpmsSerializer, indent=4)
        with open(jsonFile, "w", encoding="utf8") as file:
            file.write(json_string)
            print("ComponentPathMappings generated..!")
    except FileExistsError:
        # print("DB is locked by another instance..! Exiting current instance!")
        return False
    except PermissionError:
        # print("DB is locked by another instance..! Exiting current instance!")
        return False
    else:
        return True


if __name__ == "__main__":
    if sys.version_info < (3, 6):
        raise Exception("Script requires Python version >= 3.6")

    parser = argparse.ArgumentParser(description="MPMS Repo Mapper")
    parser.add_argument("--source", help="Path of the directory where MPMS files are placed", required=True)
    parser.add_argument(
        "--bb-url",
        help="Root URL of Bit Bucket",
        required=False,
        default="https://bitbucket.vih.infineon.com/projects/AURIXRC1MCAL/repos",
    )
    parser.add_argument(
        "--local-repo",
        help="Local repository to be used for generating the mapping table",
        required=False,
        default="C:\\git-repos\\aurix_rc1_sw_mcal",
    )
    parser.add_argument(
        "--outputdir",
        help="Output directory where ComponentPathMappings.json to be generated",
        required=False,
        default="C:\\git-repos\\aurix_rc1_sw_mcal_tools_and_scripts\\EA\\04_MpmsScripts",
    )
    args = parser.parse_args()
    main(args)
