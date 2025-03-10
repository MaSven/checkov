import os.path
from time import sleep

import boto3
import dpath.util
import json
import logging
import re
import requests
import urllib3
import webbrowser
from botocore.exceptions import ClientError
from colorama import Style
# from git import Repo
from json import JSONDecodeError
from os import path
from termcolor import colored
from tqdm import trange
from urllib3.exceptions import HTTPError

from checkov.common.bridgecrew.ci_variables import *
from checkov.common.bridgecrew.platform_errors import BridgecrewAuthError
from checkov.common.bridgecrew.platform_key import read_key, persist_key, bridgecrew_file
from checkov.common.bridgecrew.wrapper import reduce_scan_reports, persist_checks_results, \
    enrich_and_persist_checks_metadata
from checkov.common.models.consts import SUPPORTED_FILE_EXTENSIONS
from checkov.common.runners.base_runner import filter_ignored_paths
from checkov.version import version as checkov_version

EMAIL_PATTERN = r"[^@]+@[^@]+\.[^@]+"

ACCOUNT_CREATION_TIME = 180  # in seconds

UNAUTHORIZED_MESSAGE = 'User is not authorized to access this resource with an explicit deny'

DEFAULT_REGION = "us-west-2"

ONBOARDING_SOURCE = "checkov"

SIGNUP_HEADER = {
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (KHTML, like Gecko) Chrome/85.0.4183.83 Safari/537.36',
    'Content-Type': 'application/json;charset=UTF-8'
}

class BcPlatformIntegration(object):
    def __init__(self):
        self.bc_api_key = read_key()
        self.s3_client = None
        self.bucket = None
        self.credentials = None
        self.repo_path = None
        self.repo_id = None
        self.repo_branch = None
        self.skip_fixes = False
        self.skip_suppressions = False
        self.skip_policy_download = False
        self.timestamp = None
        self.scan_reports = []
        self.bc_api_url = os.getenv('BC_API_URL', "https://www.bridgecrew.cloud/api/v1")
        self.bc_source = None
        self.bc_source_version = None
        self.integrations_api_url = f"{self.bc_api_url}/integrations/types/checkov"
        self.guidelines_api_url = f"{self.bc_api_url}/guidelines"
        self.onboarding_url = f"{self.bc_api_url}/signup/checkov"
        self.api_token_url = f"{self.bc_api_url}/integrations/apiToken"
        self.suppressions_url = f"{self.bc_api_url}/suppressions"
        self.fixes_url = f"{self.bc_api_url}/fixes/checkov"
        self.guidelines = None
        self.bc_id_mapping = None
        self.ckv_to_bc_id_mapping = None
        self.use_s3_integration = False
        self.platform_integration_configured = False
        self.http = None
        self.excluded_paths = []

    def setup_http_manager(self, ca_certificate=os.getenv('BC_CA_BUNDLE', None)):
        """
        bridgecrew uses both the urllib3 and requests libraries, while checkov uses the requests library.
        :param ca_certificate: an optional CA bundle to be used by both libraries.
        """
        if self.http:
            return
        if ca_certificate:
            os.environ['REQUESTS_CA_BUNDLE'] = ca_certificate
            try:
                self.http = urllib3.ProxyManager(os.environ['https_proxy'], cert_reqs='REQUIRED', ca_certs=ca_certificate)
            except KeyError:
                self.http = urllib3.PoolManager(cert_reqs='REQUIRED', ca_certs=ca_certificate)
        else:
            try:
                self.http = urllib3.ProxyManager(os.environ['https_proxy'])
            except KeyError:
                self.http = urllib3.PoolManager()

    def setup_bridgecrew_credentials(self, bc_api_key, repo_id, skip_fixes=False, skip_suppressions=False,
                                     skip_policy_download=False, source=None, source_version=None, repo_branch=None):
        """
        Setup credentials against Bridgecrew's platform.
        :param source:
        :param skip_fixes: whether to skip querying fixes from Bridgecrew
        :param repo_id: Identity string of the scanned repository, of the form <repo_owner>/<repo_name>
        :param bc_api_key: Bridgecrew issued API key
        """
        self.bc_api_key = bc_api_key
        self.repo_id = repo_id
        self.repo_branch = repo_branch
        self.skip_fixes = skip_fixes
        self.skip_suppressions = skip_suppressions
        self.skip_policy_download = skip_policy_download
        self.bc_source = source
        self.bc_source_version = source_version

        if self.bc_source.upload_results:
            try:
                self.skip_fixes = True  # no need to run fixes on CI integration
                repo_full_path, response = self.get_s3_role(bc_api_key, repo_id)
                self.bucket, self.repo_path = repo_full_path.split("/", 1)
                self.timestamp = self.repo_path.split("/")[-1]
                self.credentials = response["creds"]
                self.s3_client = boto3.client("s3",
                                              aws_access_key_id=self.credentials["AccessKeyId"],
                                              aws_secret_access_key=self.credentials["SecretAccessKey"],
                                              aws_session_token=self.credentials["SessionToken"],
                                              region_name=DEFAULT_REGION
                                              )
                sleep(10)  # Wait for the policy to update
                self.platform_integration_configured = True
                self.use_s3_integration = True
            except HTTPError as e:
                logging.error(f"Failed to get customer assumed role\n{e}")
                raise e
            except ClientError as e:
                logging.error(f"Failed to initiate client with credentials {self.credentials}\n{e}")
                raise e
            except JSONDecodeError as e:
                logging.error(f"Response of {self.integrations_api_url} is not a valid JSON\n{e}")
                raise e

        self.get_id_mapping()

        self.platform_integration_configured = True

    def get_s3_role(self, bc_api_key, repo_id):
        request = self.http.request("POST", self.integrations_api_url, body=json.dumps({"repoId": repo_id}),
                               headers={"Authorization": bc_api_key, "Content-Type": "application/json"})
        response = json.loads(request.data.decode("utf8"))
        while ('Message' in response or 'message' in response):
            if 'Message' in response and response['Message'] == UNAUTHORIZED_MESSAGE:
                raise BridgecrewAuthError()
            if 'message' in response and "cannot be found" in response['message']:
                self.loading_output("creating role")
                request = self.http.request("POST", self.integrations_api_url, body=json.dumps({"repoId": repo_id}),
                                       headers={"Authorization": bc_api_key, "Content-Type": "application/json"})
                response = json.loads(request.data.decode("utf8"))

        repo_full_path = response["path"]
        return repo_full_path, response

    def is_integration_configured(self):
        """
        Checks if Bridgecrew integration is fully configured based in input params.
        :return: True if the integration is configured, False otherwise
        """
        return self.platform_integration_configured

    def persist_repository(self, root_dir, files=None, excluded_paths=[]):
        """
        Persist the repository found on root_dir path to Bridgecrew's platform. If --file flag is used, only files
        that are specified will be persisted.
        :param files: Absolute path of the files passed in the --file flag.
        :param root_dir: Absolute path of the directory containing the repository root level.
        """

        if not self.use_s3_integration:
            return

        if files:
            for f in files:
                _, file_extension = os.path.splitext(f)
                if file_extension in SUPPORTED_FILE_EXTENSIONS:
                    self._persist_file(f, os.path.relpath(f, root_dir))
        else:
            for root_path, d_names, f_names in os.walk(root_dir):
                # self.excluded_paths only contains the config fetched from the platform.
                # but here we expect the list from runner_registry as well (which includes self.excluded_paths).
                filter_ignored_paths(root_path, d_names, excluded_paths)
                filter_ignored_paths(root_path, f_names, excluded_paths)
                for file_path in f_names:
                    _, file_extension = os.path.splitext(file_path)
                    if file_extension in SUPPORTED_FILE_EXTENSIONS:
                        full_file_path = os.path.join(root_path, file_path)
                        relative_file_path = os.path.relpath(full_file_path, root_dir)
                        self._persist_file(full_file_path, relative_file_path)

    def persist_scan_results(self, scan_reports):
        """
        Persist checkov's scan result into bridgecrew's platform.
        :param scan_reports: List of checkov scan reports
        """
        if not self.use_s3_integration:
            return

        self.scan_reports = scan_reports
        reduced_scan_reports = reduce_scan_reports(scan_reports)
        checks_metadata_paths = enrich_and_persist_checks_metadata(scan_reports, self.s3_client, self.bucket,
                                                                   self.repo_path)
        dpath.util.merge(reduced_scan_reports, checks_metadata_paths)
        persist_checks_results(reduced_scan_reports, self.s3_client, self.bucket, self.repo_path)

    def commit_repository(self, branch):
        """
        :param branch: branch to be persisted
        Finalize the repository's scanning in bridgecrew's platform.
        """
        if not self.use_s3_integration:
            return

        request = None
        try:

            request = self.http.request("PUT", f"{self.integrations_api_url}?source={self.bc_source.name}",
                                   body=json.dumps({"path": self.repo_path, "branch": branch, "to_branch": BC_TO_BRANCH,
                                                    "pr_id": BC_PR_ID, "pr_url": BC_PR_URL,
                                                    "commit_hash": BC_COMMIT_HASH, "commit_url": BC_COMMIT_URL,
                                                    "author": BC_AUTHOR_NAME, "author_url": BC_AUTHOR_URL,
                                                    "run_id": BC_RUN_ID, "run_url": BC_RUN_URL,
                                                    "repository_url": BC_REPOSITORY_URL}),
                                   headers={"Authorization": self.bc_api_key, "Content-Type": "application/json",
                                            'x-api-client': self.bc_source.name, 'x-api-checkov-version': checkov_version
                                            })
            response = json.loads(request.data.decode("utf8"))
            url = response.get("url", None)
            return url
        except HTTPError as e:
            logging.error(f"Failed to commit repository {self.repo_path}\n{e}")
            raise e
        except JSONDecodeError as e:
            logging.error(f"Response of {self.integrations_api_url} is not a valid JSON\n{e}")
            raise e
        finally:
            if request.status == 201 and response["result"] == "Success":
                logging.info(f"Finalize repository {self.repo_id} in bridgecrew's platform")
            else:
                raise Exception(f"Failed to finalize repository {self.repo_id} in bridgecrew's platform\n{response}")

    def _persist_file(self, full_file_path, relative_file_path):
        tries = 4
        curr_try = 0
        file_object_key = os.path.join(self.repo_path, relative_file_path).replace("\\", "/")
        while curr_try < tries:
            try:
                self.s3_client.upload_file(full_file_path, self.bucket, file_object_key)
                return
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') == 'AccessDenied':
                    sleep(5)
                    curr_try += 1
                else:
                    logging.error(f"failed to persist file {full_file_path} into S3 bucket {self.bucket}\n{e}")
                    raise e
            except Exception as e:
                logging.error(f"failed to persist file {full_file_path} into S3 bucket {self.bucket}\n{e}")
                raise e
        if curr_try == tries:
            logging.error(
                f"failed to persist file {full_file_path} into S3 bucket {self.bucket} - gut AccessDenied {tries} times")

    def get_guidelines(self) -> dict:
        if not self.guidelines:
            self.get_checkov_mapping_metadata()
        return self.guidelines

    def get_id_mapping(self) -> dict:
        if not self.bc_id_mapping:
            self.get_checkov_mapping_metadata()
        return self.bc_id_mapping

    def get_ckv_to_bc_id_mapping(self) -> dict:
        if not self.ckv_to_bc_id_mapping:
            self.get_checkov_mapping_metadata()
        return self.ckv_to_bc_id_mapping

    def get_checkov_mapping_metadata(self) -> dict:
        BC_SKIP_MAPPING = os.getenv("BC_SKIP_MAPPING","FALSE")
        if BC_SKIP_MAPPING.upper() == "TRUE":
            logging.debug(f"Skipped mapping API call")
            return {}
        try:
            request = self.http.request("GET", self.guidelines_api_url)
            response = json.loads(request.data.decode("utf8"))
            self.guidelines = response["guidelines"]
            self.bc_id_mapping = response.get("idMapping")
            self.ckv_to_bc_id_mapping = {ckv_id: bc_id for (bc_id, ckv_id) in self.bc_id_mapping.items()}
            logging.debug(f"Got checkov mappings from Bridgecrew BE")
        except Exception as e:
            logging.debug(f"Failed to get the guidelines from {self.guidelines_api_url}, error:\n{e}")
            return {}

    def onboarding(self):
        if not self.bc_api_key:
            print(Style.BRIGHT + colored("\nWould you like to “level up” your Checkov powers for free?  The upgrade includes: \n\n", 'green',
                                         attrs=['bold'])  + colored(
                u"\u2022 " + "Command line docker Image scanning\n"
                u"\u2022 " + "Free (forever) bridgecrew.cloud account with API access\n"
                u"\u2022 " + "Auto-fix remediation suggestions\n"
                u"\u2022 " + "Enabling of VS Code Plugin\n"
                u"\u2022 " + "Dashboard visualisation of Checkov scans\n"
                u"\u2022 " + "Integration with GitHub for:\n"
                "\t" + u"\u25E6 " + "\tAutomated Pull Request scanning\n"
                "\t" + u"\u25E6 " + "\tAuto remediation PR generation\n"
                u"\u2022 " + "Integration with up to 100 cloud resources for:\n"
                "\t" + u"\u25E6 " + "\tAutomated cloud resource checks\n"
                "\t" + u"\u25E6 " + "\tResource drift detection\n"
                "\n"           
                "\n" + "and much more...",'yellow') + 
                colored("\n\nIt's easy and only takes 2 minutes. We can do it right now!\n\n"
                "To Level-up, press 'y'... \n",
                'cyan') + Style.RESET_ALL)
            reply = self._input_levelup_results()
            if reply[:1] == 'y':
                print(Style.BRIGHT + colored("\nOk, let’s get you started on creating your free account! \n"
                "\nEnter your email address to begin: ",'green', attrs=['bold']) + colored(" // This will be used as your login at https://bridgecrew.cloud.\n", 'green'))
                if not self.bc_api_key:
                    email = self._input_email()
                    print(Style.BRIGHT + colored("\nLooks good!"
                    "\nNow choose an Organisation Name: ",'green', attrs=['bold']) + colored(" // This will enable collaboration with others who you can add to your team.\n", 'green'))
                    org = self._input_orgname()
                    print(Style.BRIGHT + colored("\nAmazing!"
                    "\nWe are now generating a personal API key to immediately enable some new features… ",'green', attrs=['bold']))
 
                    bc_api_token, response = self.get_api_token(email, org)
                    self.bc_api_key = bc_api_token
                    if response.status_code == 200:
                        print(Style.BRIGHT + colored("\nComplete!",'green', attrs=['bold']))
                        print('\nSaving API key to {}'.format(bridgecrew_file))
                        print(Style.BRIGHT + colored("\nCheckov will automatically check this location for a key.  If you forget it you’ll find it here\nhttps://bridgecrew.cloud/integrations/api-token\n\n",'green'))
                        persist_key(self.bc_api_key)
                        print(Style.BRIGHT + colored("Checkov Dashboard is configured, opening https://bridgecrew.cloud to explore your new powers.", 'green', attrs=['bold']))
                        print(Style.BRIGHT + colored("FYI - check your inbox for login details! \n", 'green'))

                        print(Style.BRIGHT + colored("Congratulations! You’ve just super-sized your Checkov!  Why not test-drive image scanning now:",'cyan')) 

                        print(Style.BRIGHT + colored("\ncheckov --docker-image ubuntu --dockerfile-path /Users/bob/workspaces/bridgecrew/Dockerfile --repo-id bob/test --branch master\n",'white'))

                        print(Style.BRIGHT + colored("Or download our VS Code plugin:  https://github.com/bridgecrewio/checkov-vscode \n", 'cyan',attrs=['bold']))                  

                        print(Style.BRIGHT + colored( "Interested in contributing to Checkov as an open source developer.  We thought you’d never ask.  Check us out at: \nhttps://github.com/bridgecrewio/checkov/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22 \n", 'white', attrs=['bold']))   
                       
                    else:
                        print(
                            Style.BRIGHT + colored("\nCould not create account, please try again on your next scan! \n",
                                                   'red', attrs=['bold']) + Style.RESET_ALL)
                    webbrowser.open(
                        "https://bridgecrew.cloud/?utm_source=cli&utm_medium=organic_oss&utm_campaign=checkov")
            else:
                print(
                    "\n To see the Dashboard prompt again, run `checkov` with no arguments \n For Checkov usage, try `checkov --help`")
        else:
            print("No argument given. Try ` --help` for further information")

    def get_report_to_platform(self, args, scan_reports):
        if self.bc_api_key:

            if args.directory:
                repo_id = self.get_repository(args)
                self.setup_bridgecrew_credentials(bc_api_key=self.bc_api_key, repo_id=repo_id)
            if self.is_integration_configured():
                self._upload_run(args, scan_reports)

# Added this to generate a default repo_id for cli scans for upload to the platform 
# whilst also persisting a cli repo_id into the object
    def persist_bc_api_key(self, args):
        if args.bc_api_key:
            self.bc_api_key=args.bc_api_key
        else: 
            # get the key from file
            self.bc_api_key=read_key()
        return self.bc_api_key    

# Added this to generate a default repo_id for cli scans for upload to the platform 
# whilst also persisting a cli repo_id into the object
    def persist_repo_id(self, args):
        if args.repo_id is None:
            if BC_FROM_BRANCH:
                self.repo_id = BC_FROM_BRANCH
            if args.directory:
                basename = path.basename(os.path.abspath(args.directory[0]))
                self.repo_id = "cli_repo/" + basename
            if args.file:
                # Get the base path of the file based on it's absolute path
                basename = os.path.basename(os.path.dirname(os.path.abspath(args.file[0])))
                self.repo_id = "cli_repo/" + basename
 
        else: 
            self.repo_id=args.repo_id
        return self.repo_id    

    def get_repository(self, args):
        if BC_FROM_BRANCH:
            return BC_FROM_BRANCH
        basename = 'unnamed_repo' if path.basename(args.directory[0]) == '.' else path.basename(args.directory[0])
        repo_id = "cli_repo/" + basename
        return repo_id

    def get_api_token(self, email, org):
        response = self._create_bridgecrew_account(email, org)
        bc_api_token = response.json()["checkovSignup"]
        return bc_api_token, response

    def _upload_run(self, args, scan_reports):
        print(Style.BRIGHT + colored("Connecting to Bridgecrew.cloud...", 'green',
                                     attrs=['bold']) + Style.RESET_ALL)
        self.persist_repository(args.directory[0])
        print(Style.BRIGHT + colored("Metadata upload complete", 'green',
                                     attrs=['bold']) + Style.RESET_ALL)
        self.persist_scan_results(scan_reports)
        print(Style.BRIGHT + colored("Report upload complete", 'green',
                                     attrs=['bold']) + Style.RESET_ALL)
        self.commit_repository(args.branch)
        print(Style.BRIGHT + colored(
            "COMPLETE! \nYour results are in your Bridgecrew dashboard, available here: https://bridgecrew.cloud \n", 'green', attrs=['bold']) + Style.RESET_ALL)

    def _create_bridgecrew_account(self, email, org):
        """
        Create new bridgecrew account
        :param email: email of account owner
        :return: account creation response
        """
        payload = {
            "owner_email": email,
            "org": org,
            "source": ONBOARDING_SOURCE,
            "customer_name": org
        }
        response = requests.request("POST", self.onboarding_url, headers=SIGNUP_HEADER, json=payload)
        if response.status_code == 200:
            return response
        else:
            raise Exception("failed to create a bridgecrew account. An organization with this name might already "
                            "exist with this email address. Please login bridgecrew.cloud to retrieve access key");

    def _input_orgname(self):
        valid = False
        result = None
        while not valid:
            result = str(
                input(
                    'Organization name: ')).lower().strip()  # nosec
            # remove spaces and special characters
            result = ''.join(e for e in result if e.isalnum())
            if result:
                valid = True
        return result

    def _input_visualize_results(self):
        valid = False
        result = None
        while not valid:
            result = str(input('Visualize results? (y/n): ')).lower().strip()  # nosec
            if result[:1] in ["y", "n"]:
                valid = True
        return result

    def _input_levelup_results(self):
        valid = False
        result = None
        while not valid:
            result = str(input('Level up? (y/n): ')).lower().strip()  # nosec
            if result[:1] in ["y", "n"]:
                valid = True
        return result

    def _input_email(self):
        valid_email = False
        while not valid_email:
            email = str(input('E-Mail: ')).lower().strip()  # nosec
            if re.search(EMAIL_PATTERN, email):
                valid_email = True
            else:
                print("email should match the following pattern: {}".format(EMAIL_PATTERN))
        return email

    @staticmethod
    def loading_output(msg):
        with trange(ACCOUNT_CREATION_TIME) as t:
            for _ in t:
                t.set_description(msg)
                t.set_postfix(refresh=False)
                sleep(1)

    def get_excluded_paths(self):
        repo_settings_api_url = f'{self.bc_api_url}/vcs/settings/scheme'
        try:
            request = self.http.request("GET", repo_settings_api_url,
                                        headers={"Authorization": self.bc_api_key, "Content-Type": "application/json"})
            response = json.loads(request.data.decode("utf8"))
            if 'scannedFiles' in response:
                for section in response.get('scannedFiles').get('sections'):
                    if self.repo_id in section.get('repos') and section.get('rule').get('excludePaths'):
                        self.excluded_paths.extend(section.get('rule').get('excludePaths'))
            return self.excluded_paths
        except HTTPError as e:
            logging.error(f"Failed to get vcs settings for repo {self.repo_path}\n{e}")
            raise e
        except JSONDecodeError as e:
            logging.error(f"Response of {repo_settings_api_url} is not a valid JSON\n{e}")
            raise e


bc_integration = BcPlatformIntegration()
