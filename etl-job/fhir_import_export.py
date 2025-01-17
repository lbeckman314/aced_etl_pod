import os
import logging
import pathlib
import sys
import json
import subprocess

from aced_submission.meta_flat_load import DEFAULT_ELASTIC
from gen3.auth import Gen3Auth

from aced_submission.fhir_store import fhir_get
from gen3_util.config import Config
from gen3_util.meta.uploader import cp

logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def _get_token() -> str:
    """Get ACCESS_TOKEN from environment"""
    # print("[out] retrieving access token...")
    return os.environ.get('ACCESS_TOKEN', None)


def _auth(access_token: str) -> Gen3Auth:
    """Authenticate using ACCESS_TOKEN"""
    # print("[out] authorizing...")
    if access_token:
        # use access token from environment (set by sower)
        return Gen3Auth(refresh_file=f"accesstoken:///{access_token}")
    # no access token, use refresh token set in default ~/.gen3/credentials.json location
    return Gen3Auth()


def _user(auth: Gen3Auth) -> dict:
    """Get user info from arborist"""
    return auth.curl('/user/user').json()


def _input_data() -> dict:
    """Get input data"""
    assert 'INPUT_DATA' in os.environ, "INPUT_DATA not found in environment"
    return json.loads(os.environ['INPUT_DATA'])


def _get_program_project(input_data) -> tuple:
    """Get program and project from input_data"""
    assert 'project_id' in input_data, "project_id not found in INPUT_DATA"
    assert '-' in input_data['project_id'], 'project_id must be in the format <program>-<project>'
    return input_data['project_id'].split('-')


def _get_object_id(input_data) -> str:
    """Get object_id from input_data"""
    return input_data.get('object_id', None)


def _can_create(output, program, user) -> bool:
    """Check if user can create a project in the given program.

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        user: user dict from arborist (aka profile)
    """

    can_create = True

    if f"/programs/{program}" not in user['resources']:
        output['logs'].append(f"/programs/{program} not found in user resources")
        can_create = False

    required_resources = [
        '/services/sheepdog/submission/program',
        '/services/sheepdog/submission/project',
        f"/programs/{program}/projects"
    ]
    for required_resource in required_resources:
        if required_resource not in user['resources']:
            output['logs'].append(f"{required_resource} not found in user resources")
            can_create = False
        else:
            output['logs'].append(f"HAS RESOURCE {required_resource}")

    required_services = [
        f"/programs/{program}/projects"
    ]
    for required_service in required_services:
        if required_service not in user['authz']:
            output['logs'].append(f"{required_service} not found in user authz")
            can_create = False
        else:
            if {'method': '*', 'service': 'sheepdog'} not in user['authz'][required_service]:
                output['logs'].append(f"sheepdog not found in user authz for {required_service}")
                can_create = False
            else:
                output['logs'].append(f"HAS SERVICE sheepdog on resource {required_service}")

    return can_create


def _can_read(output, program, project, user) -> bool:
    """Check if user can read a project in the given program.

    Args:
        output: output dict the json that will be returned to the caller
        program: program Gen3 program(-project)
        user: user dict from arborist (aka profile)
    """

    can_read = True

    if f"/programs/{program}" not in user['resources']:
        output['logs'].append(f"/programs/{program} not found in user resources")
        can_read = False

    required_resources = [
        f"/programs/{program}/projects/{project}"
    ]
    for required_resource in required_resources:
        if required_resource not in user['resources']:
            output['logs'].append(f"{required_resource} not found in user resources")
            can_read = False
        else:
            output['logs'].append(f"HAS RESOURCE {required_resource}")

    required_services = [
        f"/programs/{program}/projects/{project}"
    ]
    for required_service in required_services:
        if required_service not in user['authz']:
            output['logs'].append(f"{required_service} not found in user authz")
            can_read = False
        else:
            if {'method': 'read-storage', 'service': '*'} not in user['authz'][required_service]:
                output['logs'].append(f"read-storage not found in user authz for {required_service}")
                can_read = False
            else:
                output['logs'].append(f"HAS SERVICE read-storage on resource {required_service}")

    return can_read


def _download_and_unzip(object_id, file_path, output) -> bool:
    """Download and unzip object_id to file_path"""
    cmd = f"gen3_util files cp {object_id} /tmp/{object_id}".split()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        output['logs'].append(f"ERROR DOWNLOADING {object_id} /tmp/{object_id}")
        if result.stderr:
            output['logs'].append(result.stderr.read().decode())
        if result.stdout:
            output['logs'].append(result.stdout.read().decode())
        return False
    output['logs'].append(f"DOWNLOADED {object_id} {file_path}")
    cmd = f"unzip -o -j /tmp/{object_id}/*.zip -d {file_path}".split()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        output['logs'].append(f"ERROR UNZIPPING /tmp/{object_id}")
        if result.stderr:
            output['logs'].append(result.stderr.read().decode())
        if result.stdout:
            output['logs'].append(result.stdout.read().decode())
        return False

    output['logs'].append(f"UNZIPPED {file_path}")
    return True


def _load_all(study, project_id, output) -> bool:
    """Use script to load study."""
    cmd = f"./load_all".split()
    output['logs'].append(f"LOADING: {cmd}")
    my_env = os.environ.copy()
    my_env['study'] = study
    my_env['project_id'] = project_id
    my_env['schema'] = 'https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json'

    result = subprocess.run(cmd, env=my_env, capture_output=True, text=True)
    if result.returncode != 0:
        output['logs'].append(f"ERROR LOADING {study}")
        output['logs'].append(result.stderr)
        output['logs'].append(result.stdout)
        return False

    output['logs'].append(f"LOADED {study}")
    output['logs'].append(result.stderr)
    output['logs'].append(result.stdout)
    return True


def _get(input_data, output, program, project, user) -> str:
    """Export data from the fhir store to bucket, returns object_id."""
    can_read = _can_read(output, program, project, user)
    if not can_read:
        output['logs'].append(f"No read permissions on {program}-{project}")
        return None

    study_path = f"studies/{project}"
    project_id = f"{program}-{project}"

    logs = fhir_get(f"{program}-{project}", study_path, DEFAULT_ELASTIC)
    output['logs'].extend(logs)

    # zip and upload the exported files to bucket
    config = Config()
    cp_result = cp(config=config, from_=study_path, project_id=project_id, ignore_state=False)
    output['logs'].append(cp_result['msg'])
    object_id = cp_result['object_id']

    return object_id


def _main():
    """Main function"""

    token = _get_token()
    auth = _auth(token)

    # print("[out] authorized successfully")

    # print("[out] retrieving user info...")
    user = _user(auth)

    output = {'user': user, 'files': [], 'logs': []}

    # output['env'] = {k: v for k, v in os.environ.items()}

    input_data = _input_data()
    program, project = _get_program_project(input_data)

    method = input_data.get("method", None)
    assert method, "input data must contain a `method`"
    if method.lower() == 'put':
        # read from bucket, write to fhir store
        _put(input_data, output, program, project, user)
    elif method.lower() == 'get':
        # read fhir store, write to bucket
        object_id = _get(input_data, output, program, project, user)
        output['object_id'] = object_id
    else:
        raise Exception(f"unknown method {method}")

    # note, only the last output (a line in stdout with `[out]` prefix) is returned to the caller
    print(f"[out] {json.dumps(output, separators=(',', ':'))}")


def _put(input_data, output, program, project, user):
    """Import data from bucket to graph, flat and fhir store."""
    # check permissions
    can_create = _can_create(output, program, user)
    output['logs'].append(f"CAN CREATE: {can_create}")
    file_path = f"/root/studies/{project}/"
    if can_create:
        object_id = _get_object_id(input_data)
        if object_id:
            # get the meta data file
            if _download_and_unzip(object_id, file_path, output):

                # tell user what files were found
                for _ in pathlib.Path(file_path).glob('*'):
                    output['files'].append(str(_))

                # load the study into the database and elastic search
                _load_all(project, f"{program}-{project}", output)

        else:
            output['logs'].append(f"OBJECT ID NOT FOUND")


if __name__ == '__main__':
    _main()
