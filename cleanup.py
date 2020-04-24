# usage: python3 cleanup.py CONNECT_STR

import os, uuid, datetime, sys
import git
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient

# set up git repo
cloned_repo = git.Repo('mathlib')

branch_heads = set([r.commit.hexsha for r in cloned_repo.refs])
master_commits = set([c.hexsha for c in cloned_repo.iter_commits('master')])


#save non-head, non-master commits for this many seconds
DURATION = datetime.timedelta(days=3) 
current_time = datetime.datetime.now(datetime.timezone.utc)

def is_deletable(sha, creation_time):
    return sha not in branch_heads \
           and sha not in master_commits \
           and current_time - creation_time > DURATION


# azure configuration
connect_str = sys.argv[1]
blob_service_client = BlobServiceClient.from_connection_string(connect_str)
container_client = blob_service_client.get_container_client('mathlib')


def get_deletable_blobs():
    blob_list = list(container_client.list_blobs())
    deletable = [blob for blob in blob_list if is_deletable(blob.name[:-7], blob.last_modified)]

    print(len(deletable), 'out of', len(blob_list), 'can be deleted, so we keep', 
          len(blob_list) - len(deletable))

    on_master = [blob for blob in blob_list if blob.name[:-7] in master_commits]
    branch_head = [blob for blob in blob_list if blob.name[:-7] in branch_heads]
    new = [blob for blob in blob_list if current_time - blob.last_modified < DURATION]

    print(len(on_master), 'are commits to master')
    print(len(branch_head), 'are branch heads')
    print(len(new), 'are too young to delete')

    return deletable

def delete_azure_blob(blob):
    print('disabled for testing')
    #container_client.delete_blob(blob)

def delete_azure_blobs(blobs):
    for b in blobs:
        print('deleting', b.name)
        delete_azure_blob(b)
    print('deleted', len(blobs), 'archives')

deletable = get_deletable_blobs()

delete_azure_blobs(deletable)