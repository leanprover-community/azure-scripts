# usage: python3 cleanup.py CONNECT_STR GITHUB_TOKEN

import os, uuid, datetime, sys
import git
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient

# set up git repo
cloned_repo = git.Repo('mathlib')

mathlib_branch_heads = set([r.commit.hexsha for r in cloned_repo.refs])
mathlib_master_commits = set([c.hexsha for c in cloned_repo.iter_commits('master')])
external_repo_info = {}
github_token = sys.argv[2]

#save non-head, non-master commits for this many seconds
DURATION = datetime.timedelta(days=3)
current_time = datetime.datetime.now(datetime.timezone.utc)

def is_deletable_mathlib(sha, creation_time):
    return sha not in mathlib_branch_heads \
            and sha not in mathlib_master_commits \
            and current_time - creation_time > DURATION

# archives from e.g. lean-liquid are only saved if they come from recent master commits
def is_deletable_external(repo, sha, creation_time):
    if repo not in external_repo_info:
        new_cloned_repo = git.Repo.clone_from(f'https://{github_token}@github.com/leanprover-community/{repo}.git',repo)
        external_repo_info[repo] = {
            'branch_heads': set(r.commit.hexsha for r in new_cloned_repo.refs),
            'master_commits': set(c.hexsha for c in new_cloned_repo.iter_commits('master')),
            'master_head': new_cloned_repo.rev_parse('origin/master'),
        }
    return sha not in external_repo_info[repo]['master_commits'] or \
        (current_time - creation_time > DURATION and sha != external_repo_info[repo]['master_head'])

def is_deletable(path, creation_time):
    if '/' not in path: # this archive came from mathlib
        return is_deletable_mathlib(path, creation_time)
    else:
        components = path.split('/')
        return is_deletable_external(components[0], components[-1], creation_time)


# azure configuration
connect_str = sys.argv[1]
blob_service_client = BlobServiceClient.from_connection_string(connect_str)
container_client = blob_service_client.get_container_client('mathlib')

def get_deletable_blobs():
    blob_list = list(container_client.list_blobs())
    deletable = [blob for blob in blob_list if is_deletable(blob.name[:-7], blob.last_modified)]

    print(len(deletable), 'out of', len(blob_list), 'can be deleted, so we keep',
          len(blob_list) - len(deletable))

    on_master = [blob for blob in blob_list if blob.name[:-7] in mathlib_master_commits]
    branch_head = [blob for blob in blob_list if blob.name[:-7] in mathlib_branch_heads]
    new = [blob for blob in blob_list if current_time - blob.last_modified < DURATION]

    print(len(on_master), 'are commits to master')
    print(len(branch_head), 'are branch heads')
    print(len(new), 'are too young to delete')

    return deletable

def delete_azure_blob(blob):
    container_client.delete_blob(blob)

def delete_azure_blobs(blobs):
    for b in blobs:
        print('deleting', b.name)
        delete_azure_blob(b)
    print('deleted', len(blobs), 'archives')

deletable = get_deletable_blobs()

delete_azure_blobs(deletable)
