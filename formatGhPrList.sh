#!/usr/bin/env bash

# Running `./formatGhPrList.sh <linkifier>` produces an .md-formatted table
# of all the issues in the current repository.
# Assuming that `<linkifier>#NNN` is a valid Zulip linkifier, the PRs will be clickable links.

# This is the start of the linkifier, without the `#`, e.g. `blog` or `website`.
repo="${1:-linkifier}"

# The output of `gh pr list` is a 5-column, tab-separated table containing
# ID TITLE BRANCH STATUS DATE
gh pr list |
  awk -F$'\t' -v link="${repo}#" 'BEGIN{
    # Set the default separator to be `|` to match an md-table formatting
    OFS="|"
    # Print the Headers of the table
    print "", " ID ", " TITLE ", " DATE ", " STATUS ", ""
    # Print the alignments of the table
    print "", " - ", " - ", " - ", " - ", ""
  }
    # Skip the 3rd field, containing the branch name, remove `T<time>` from the date entry.
    {
      date=$5
      gsub(/T.*/, "", date)
      print "", link $1, $2, date, $4, ""
    }'
