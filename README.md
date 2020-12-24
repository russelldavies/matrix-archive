# matrix-archive

Archive Matrix room messages.

Creates a YAML log of all room messages, including media.

# Installation

Note that at least Python 3.8+ is required.

1. Install [libolm](https://gitlab.matrix.org/matrix-org/olm) 3.1+

    - Debian 11+ (testing/sid) or Ubuntu 19.10+: `sudo apt install libolm-dev`

    - Archlinux based distribution can install the `libolm` package from the Community repository

    - macOS `brew install libolm`

    - Failing any of the above refer to the [olm
      repo](https://gitlab.matrix.org/matrix-org/olm) for build instructions.

2. Clone the repo and install dependencies
    ```
    git clone git@github.com:russelldavies/matrix-archive.git
    cd matrix-archive
    # Optional: create a virtualenv
    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    ```

# Usage

1. Download your E2E room keys: in the client Element you can do this by
   clicking on your profile icon, _Security & privacy_, _Export E2E room keys_.

2.  Run with an optional directory to store output, e.g.: `./matrix-archive.py chats`. Arguments can be passed in the environment variables: `MX_HOMESERVER`, `MX_USERID`, `MX_KEYS_PATH`, `MX_KEYS_PASS`.

3. You'll be prompted to enter your homeserver, user credentials and the path
   to the room keys you downloaded in step 1.

4. You'll be prompted to select which room you want to archive and a YAML file
   with a log of all messages will be written along with media and member
   avatars.
