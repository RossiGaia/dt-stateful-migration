#!/bin/sh

echo 'Checking env vars to see if it necessary to perform rsync.'

# check if RSYNC_SOURCE is set and not empty
if [ -n "${RSYNC_SOURCE}" ]; then
    echo 'Performing rsync.'

    # checking if other infos are given
    if [ -z "${RSYNC_SOURCE_PATH}" ]; then
        echo 'Rsync source path not set.'
        exit 1
    fi

    if [ -z "${RSYNC_DEST_PATH}" ]; then
        echo 'Rsync destination path not set.'
        exit 1
    fi

    # performing rsync
    echo "Performing rsync $RSYNC_SOURCE::$RSYNC_SOURCE_PATH $RSYNC_DEST_PATH"
    rsync "$RSYNC_SOURCE::$RSYNC_SOURCE_PATH" "$RSYNC_DEST_PATH"

    # checking errors
    if [ $? -eq 0 ]; then
        echo 'Rsync performed correctly.'
    else
        echo 'Rsync failed.'
    fi

else
    echo 'Not performing rsync.'
fi

exit 0
