# hearth

<img width="2729" height="820" alt="hearth_logo" src="https://github.com/user-attachments/assets/6f70597e-a689-4045-8aef-7bafc60b43c0" />


# Overview

hearth is a Python script to help mass download an Anna's Archive List. It has been made with the help of Gemini, but all code has been revised and tested by me.

**Key features**:
- This is a terminal tool that accepts command line parameters to function (more about usage below).
- hearth supports Anna's Archive List links in the form of `https://annas-archive.XX/list/<list_id>` as well as importing a list of Anna's Archive links from a `.txt` file.
- The tool will spin up a virtual browser that physically visits the link page, waits for the download cooldown and renames the downloaded file, before going ahead to the next List element, logging successes and failures in specific files.
- These files allow you to not only stop the script mid-way, closing the terminal windows completely, and then resuming from the last link it successfully downloaded (by using the same exact command), but it also allows to retry for failed links once the tool has finished processing the whole queue.
- You can use the `completed.txt` file that the script will create in your download directory as an index of all the files you downloaded as well as their md5 code.
- The download destination folder is chosen via command line parameters. Here will be stored said files.
- You can set how to rename the downloaded files, based on how much information you want to be in the filename, via command line parameters.

# Installation and launch

## Python installation and installation of necessary tools
This is a Python script. You will need to have Python (and Playwright) installed for it to run.

### On Windows:
**Python installation**:

You can install Python by going to https://www.python.org/downloads/ and downloading the installer for the latest version.  
Be sure to check the box that says "Add python.exe to PATH" before clicking on "Install Now" (if you skip this, the script won't work.).

Alternatively, you can install it via command prompt using Winget.  
Open CMD and paste this command:
```
winget install Python.Python.3.12 --source winget
```
substituting `3.12` with the latest version.

Alternatively, open CMD, type `python`, and press Enter. This will automatically open the Microsoft Store to the most up-to-date version of Python available for your system.

**Installation of necessary tools**:

The script needs a tool called "Playwright" to control the web browser.

To install it, open CMD and paste this command:
```
python -m pip install playwright
```
Once that finishes downloading and installing, paste this one:
```
python -m playwright install chromium
```
With this, we have everything necessary.

### On Linux:
**Python installation**:

Most Linux distributions already have Python installed. You just need to ensure you have `pip` (the package installer) to download the necessary tools.  
Open your terminal and run the following command (this example uses `apt` for Debian/Ubuntu-based systems, but you should easily be able to find the specific command for your distro):
```
sudo apt update && sudo apt install python3 python3-pip
```

**Installation of necessary tools**:

The script needs a tool called "Playwright" to control the web browser, plus a few system dependencies to run it.  
Open your terminal and run these commands one by one:
```
pip3 install playwright
playwright install chromium
playwright install-deps
```
*Note - IN CASE OF ERROR: if `pip3 install playwright` raises an `externally-managed-environment` error, running `pip3 install playwright --break-system-packages` instead should fix it.*  
*Note: the `install-deps` command might ask for your system password to install missing browser libraries.*

Note: Calling `playwright` directly can sometimes fail if `~/.local/bin` isn't in the user's shell `$PATH`. Using `python3 -m` for all commands should circumvent this for most Linux environments:

```
python3 -m pip install playwright
python3 -m playwright install chromium
python3 -m playwright install-deps
```

With this, we have everything necessary.

## Downloading the script and launch

### Downloading the script and folder set-up
1. Create a new folder named `hearth` or `AAdownloaderScript` (or the name you prefer) on your computer.
2. Download `hearth.py` from this repository and put the file in the folder you just created.
3. (Optional) inside of the folder, make another folder in which your downloaded files will go. Name this second folder `AAdownloads` or anything you deem fit.

### Script launch

To launch hearth, simply open your terminal directly inside of the folder that contains `hearth.py` or `cd` into it (the command should be `cd <pathToHearthFolder>`) and paste the following command:
```
python hearth.py
```
*ATTENTION: as older versions of Linux used to ship with both Python 2 and Python 3, the launch command on Linux is almost always `python3`, not just `python`. Linux users should type `python3 hearth.py` instead. Assume this for the next steps.*

# Usage

Simply launching the script with `python hearth.py` will do nothing, as the script has no parameters (such as your List or your `.txt` file, the operating mode, the chosen download directory and the file naming options)

To correctly use hearth, follow this scheme:
```
python hearth.py <OPERATING_MODE/LIST_LINK> <DOWNLOAD_FOLDER_DIRECTORY> [FILENAME_FORMAT]
```
These are the options you have:

## <OPERATING_MODE/LIST_LINK>

Here you can put one of the following parameters:

- The link to your Anna's Archive List. An example is `https://annas-archive.XX/list/<list_id>`.
- `text`. This tells the script to not expect a link to a List and instead read the AA links directly from a `.txt` file, named `aa_links.txt`, that you will put in the `AAdownloads` folder (or what you've called it).
- `retry`. To be used after hearth has already completed a queue of links, be it from a List link or `aa_links.txt`, and has failed to download some of them. This tells the script to retry all of the downloads that failed from the previous queue. ATTENTION: This requires the downloads folder to be the same as the previous operation (the one of which you want to retry the failed links).

## <DOWNLOAD_FOLDER_DIRECTORY>

Here you have to put the directory of the folder `AAdownloads` (or the name you chose for it).

If you hadn't created it until now, it will be created once you launch the script.

**Windows example**:
```
"C:\hearth\AAdownloads"
```

**Linux example**:
```
~/hearth/AAdownloads
```
or (as in some Linux shells (like Bash or Zsh), tilde expansion does not work inside double quotes)
```
"/home/<your username>/hearth/AAdownloads"
```

## [FILENAME_FORMAT]

When you download a file from Anna's Archive, the site gives you a suggested filename with a lot of information such as Title, Author(s), Year of publication, Publisher, ISBN, md5 code, and more.
This parameter tells the script which ones of these you want to be present in the name of your files.

You can leave it blank (which is the default and corresponds to `full`) or put one of the following parameters:
- `full`. This tells the script to save in the file's name all the info that Anna's Archive suggests, including the md5 code and "Anna's Archive" at the end.
- `info`. This tells the script to save in the file's name all the info that Anna's Archive suggests, however excluding the md5 code and "Anna's Archive" at the end.
- `author`. This tells the script to save only the Title and the Author, in which case a hyphen will be put between the two.
- `title`. This tells the script to save only the Title of the media you're downloading.

## IMPORTANT STEP: CAPTCHAS

As the browser controlled by Playwright is automated, it cannot solve Captchas.

At the launch of the script, when the main List link is loaded or when a mirror "slow download" link is loaded for the first time, the browser might ask you to solve a Captcha. The script will wait for you to do so.

Normally, solving the Captcha that appears at the load of the main List page as well as the one that appears at the first load of a mirror "slow download" link **_is enough for the rest of the session_**.

# What this script supports

This script officially supports and has been tested with:

- Files present on normal "slow download" mirrors, such as these:
  <img width="1257" height="636" alt="AA normal slow download mirror" src="https://github.com/user-attachments/assets/abf0f76d-6654-462b-855e-0e082368767e" />

- Files present on Libgen mirrors, such as these:
  <img width="1271" height="481" alt="AA libgen mirror" src="https://github.com/user-attachments/assets/1aa82bec-ad65-4108-8a03-2a7464687602" />

# License

This project is under the MIT license. Check out `LICENSE` for more details.

# Issues and suggestions

This project is still new, I know little about Python and I have a lot to learn.

If you have any issues or bugs to report, please feel free to do so. Same thing goes for suggestions or improvements.


