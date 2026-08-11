import os
import sys
import json
import time
import re
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

'''
# ==============
# default config
# ==============
DOWNLOAD_DIR = r"<pathToYourFolder>"

TXT_MODE = False
RETRY_MODE = False
LIST_URL = "https://annas-archive.XX/list/<list_id>"
NAME_FORMAT = "full"
'''

# ==========================
# command line configuration
# ==========================
#terminal commands reading and setting all variables
if 3 <= len(sys.argv) <= 4:
    arg1 = sys.argv[1].strip()
    DOWNLOAD_DIR = sys.argv[2].strip()
    
    #Check for the optional 4th argument (naming format)
    if len(sys.argv) == 4:
        parsed_format = sys.argv[3].strip().lower() #forces the parameter to lowercase
        if parsed_format in ["full", "info", "author", "title"]:
            NAME_FORMAT = parsed_format
        else:
            print(f"\n[ERROR] Invalid file naming format '{parsed_format}'.")
            print("Please use one of: 'full', 'info', 'author', or 'title'.")
            sys.exit(1)
    else:
        NAME_FORMAT = "full"
    
    if arg1.lower() == "text":
        TXT_MODE = True
        RETRY_MODE = False
    elif arg1.lower() == "retry":
        RETRY_MODE = True
        TXT_MODE = False
    else:
        #if it's not 'text' or 'retry', assume it's a list URL
        #URL VALIDATION CHECK ADDED HERE
        if not (arg1.startswith("http") and "annas-archive." in arg1.lower()):
            print(f"\n[ERROR] Invalid URL provided!")
            print(f"'{arg1}' does not appear to be a valid Anna's Archive link.")
            print("Please provide a valid link, or use 'text' or 'retry' mode.")
            sys.exit(1)
            
        LIST_URL = arg1
        TXT_MODE = False
        RETRY_MODE = False
        
else:
    print("Invalid arguments! Please use one of the following formats:")
    print('  python auto_downloader.py "https://annas-archive.XX/list/<list_id>" "<pathToYourFolder>" [filename format]')
    print('  python auto_downloader.py text "<pathToYourFolder>" [filename format]')
    print('  python auto_downloader.py retry "<pathToYourFolder>" [filename format]')
    print('\nAvailable file naming formats (optional):')
    print("full (all known file info) [default],")
    print("info (all known file info without the md5 code and the Anna's Archive source),")
    print("author (Title and Author, in which case a hyphen will be put between the two),")
    print("title (just the Title)")
    sys.exit(1)

# these must be defined AFTER DOWNLOAD_DIR is updated by the terminal arguments
TXT_FILE = os.path.join(DOWNLOAD_DIR, "aa_links.txt")
COMPLETED_FILE = os.path.join(DOWNLOAD_DIR, "completed.txt")
FAILED_FILE = os.path.join(DOWNLOAD_DIR, "failed_downloads.json")
# ============================================================================

def load_completed():
    if os.path.exists(COMPLETED_FILE):
        with open(COMPLETED_FILE, "r", encoding="utf-8") as f: #"r" = reading mode
            return set(line.split(" ||| ")[0].strip() for line in f if line.strip()) #grabs the url which is before the " ||| "
    return set()

def mark_completed(url, title):
    with open(COMPLETED_FILE, "a", encoding="utf-8") as f: #"a" = append mode -> adds on another line
        safe_title = title.replace('\n', ' ').replace('\r', '').strip() #cleans "\" type characters (\n (enter) and \r (line break)) so they don't break the .txt
        f.write(f"{url} ||| {safe_title}\n") #saves

def save_failed_log(failed_items):
    with open(FAILED_FILE, "w", encoding="utf-8") as f: #"w" = writing mode, used for the json. not "a" (append) because writing the json structure is more complicated 
        json.dump(failed_items, f, indent=4, ensure_ascii=False) #saves failed links in the json

def load_failed_log():
    if os.path.exists(FAILED_FILE):
        with open(FAILED_FILE, "r", encoding="utf-8") as f: #loads the json
            try:
                return json.load(f)
            except Exception: #if file is corrupted, move on
                return []
    return []

def find_valid_link(page, text_match=None, href_match=None): #function search for links in the page
    ##Finds the first link containing the text or href that is NOT a dummy '#' link.
    selectors = []
    if text_match:
        selectors.append(f"a:has-text('{text_match}')")
    if href_match:
        selectors.append(f"a[href*='{href_match}']")
        
    combined_selector = ", ".join(selectors)
    
    for el in page.locator(combined_selector).all():
        h = el.get_attribute("href")
        if h and h not in ["#", "", "javascript:void(0)", "javascript:"]: #this ensures it's not a fake button
            return el
    return None

def clean_downloaded_title(filename, url):
    ##Cleans the downloaded filename by removing extensions, MD5 hashes, and Anna's Archive tags.
    name = os.path.splitext(filename)[0] #removes extension
    
    md5_hash = url.rstrip('/').split('/')[-1] #identifies md5 code
    if len(md5_hash) == 32:
        name = re.sub(md5_hash, "", name, flags=re.IGNORECASE) #removes md5 code
        
    name = re.sub(r"[-_]*\s*Anna['’`\s]?s\s*Archive", "", name, flags=re.IGNORECASE) #removes the "Anna's Archive" text
    name = name.replace("[]", "").replace("()", "")
    return name.strip(" -_")

def generate_custom_filename(original_name, url, format_type):
    ##Generates the final filename based on the user's terminal choice.
    if format_type == "full":
        return original_name
        
    #get the file extension (e.g., .cbr, .epub)
    ext = os.path.splitext(original_name)[1]
    
    #get the cleaned base string (Title - Author - Publisher)
    clean_base = clean_downloaded_title(original_name, url)
    
    if format_type == "info":
        return f"{clean_base}{ext}"
        
    #splitting ONLY by exact double hyphens surrounded by spaces
    parts = clean_base.split(" -- ")
    
    if format_type == "author":
        #check if there are at least 2 fields to grab
        if len(parts) >= 2:
            return f"{parts[0].strip()} - {parts[1].strip()}{ext}"
        else:
            return f"{clean_base}{ext}"
            
    if format_type == "title":
        return f"{parts[0].strip()}{ext}"
        
    return original_name

def main():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    completed_urls = load_completed() #loads the content of completed.txt in a set (array) which is in ram (called "completed_urls")
    failed_items = load_failed_log() #does the same with failed_downloads.json (set is called "failed_items")
    
    #session tracking counters
    session_success = 0
    session_failed = 0
    session_skipped = 0
    
    print("\n~ Welcome to hearth - an Anna's Archive automatic List bulk download script ~\n")

    print("Booting up the automation browser...")
    
    with sync_playwright() as p: #starts the browser engine
        browser = p.chromium.launch(headless=False) #headless false to solve captchas
        context = browser.new_context(accept_downloads=True, locale="en-US") #accepts downloads is obvious, sets language to ensure buttons have the correct text for identification
        page = context.new_page()

        # 1. Target Selection
        if TXT_MODE:
            print(f"--- TXT MODE ACTIVE ---")
            if not os.path.exists(TXT_FILE):
                print(f"Cannot find {TXT_FILE}. Please create it and paste your links.")
                return
            with open(TXT_FILE, "r", encoding="utf-8") as f: #opens file in read mode
                unique_links = [line.strip() for line in f if line.strip()] #sets link target, strips links from the file
            targets = [{"url": link, "title": "Unknown"} for link in unique_links] #takes the urls as raw strings and puts them in the "struct" -> reformats data as the retry mode or list link mode. title is set to unknown because it's not part of the link (specified for data uniformity -> the format is equal to the other modes and is compatible with the for loop).
            print(f"Loaded {len(targets)} links from text file.\n")
            
        elif RETRY_MODE:
            print(f"--- RETRY MODE ACTIVE ---")
            targets = load_failed_log() #opens the failed_items set (array), saves the content in the targets array
            if not targets:
                print("No failed items found to retry! Exiting.")
                return
            print(f"Found {len(targets)} failed items to retry.\n")
            
        else:
            print(f"Loading your list: {LIST_URL}")
            print("  [*] Note: If a CAPTCHA appears, please solve it. The script will wait up to 2 minutes.")
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
            
            # --- CAPTCHA WAITING LOGIC ---
            try:
                page.wait_for_selector("main", state="attached", timeout=120000) #waits 2 mins for core html to appear to give time to solve the captcha
                page.wait_for_timeout(2000)
            except PlaywrightTimeoutError:
                pass
            # -----------------------------
            
            hrefs = page.eval_on_selector_all("main a[href*='/md5/']", "elements => elements.map(e => e.href)") #grabs all md5 links FROM MAIN (not from the other parts of the site, e.g. the recently downloaded carousel)
            unique_links = []
            for href in hrefs:
                if href not in unique_links: #removes duplicates
                    unique_links.append(href)
            targets = [{"url": link, "title": "Unknown"} for link in unique_links] #put all extracted links into a "targets" array. title is set to unknown (one of the reasons is in case the download fails, to write the failure in the json)
            print(f"Found {len(targets)} total items in list.\n")
            if len(targets) == 0:
                print("Your link is probably mistyped, or your List is not accessible.")
                print("Is Anna's Archive available in your country?\n")
                print("Did you complete the Captcha within the time limit?")
                return

        # 2. Download Execution
        for idx, item in enumerate(targets, 1):
            url = item["url"]
            
            if url in completed_urls: #checks if the url is in the completed.txt file, in which case it skips it
                print(f"[{idx}/{len(targets)}] Skipping already completed item: {url}")
                session_skipped += 1
                continue

            print(f"[{idx}/{len(targets)}] Processing: {url}")
            failure_reason = None
            page_title = "Unknown"

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                
                try:
                    page_title = page.title().split(" - ")[0].strip() #gets the page's title as backup
                except Exception:
                    pass

                #looks for download options
                slow_link_locator = find_valid_link(page, text_match='Slow Partner Server')
                libgen_link_locator = find_valid_link(page, text_match='Libgen.li', href_match='libgen.li')

                # ------------------------------------------------
                # Option 1 - if there is an AA Slow Partner Server
                # ------------------------------------------------
                if slow_link_locator:
                    print("  [*] Found standard Slow Partner Server link.")
                    
                    #goes to the mirror link, waits for it to load
                    href = slow_link_locator.get_attribute("href")
                    if href.startswith("/"):
                        parsed = urlparse(page.url)
                        href = f"{parsed.scheme}://{parsed.netloc}{href}"
                        
                    print(f"  [*] Navigating directly to: {href}")
                    page.goto(href, wait_until="domcontentloaded", timeout=60000)
                    
                    print("  [*] Waiting for the 'Download now' button (Timer/Captcha)...")
                    download_btn = page.locator("a:has-text('Download now')").first
                    
                    try:
                        download_btn.wait_for(state="attached", timeout=180000) #waits for a captcha resolution AND/OR for the timer
                    except PlaywrightTimeoutError:
                        failure_reason = "AA Download button never appeared (Timeout) or the Captcha wasn't solved"
                        raise Exception(failure_reason)
                    
                    print("  [*] Attempting download from AA...")
                    try:
                        with page.expect_download(timeout=15000) as download_info: #allows to download the file -> tells browser to expect one
                            download_btn.click(force=True)
                        
                        download = download_info.value
                        original_name = download.suggested_filename #gets the AA suggested name
                        
                        #generates the file name based on the terminal argument
                        file_name = generate_custom_filename(original_name, url, NAME_FORMAT)
                        file_path = os.path.join(DOWNLOAD_DIR, file_name)
                        
                        print(f"  [*] Saving {file_name}...")
                        download.save_as(file_path)
                        print(f"  [+] Success! Saved: {file_name}\n")
                            
                        #generates the clean title from the actual downloaded file for the log
                        clean_title = clean_downloaded_title(original_name, url)
                        mark_completed(url, clean_title) #writes to the completed.txt file (append mode)
                        completed_urls.add(url) #updates set (array) in ram -> doesn't repeat the same link attempt
                        
                        existing_failed = next((f for f in failed_items if f["url"] == url), None) #removes file/url from the failed items json in case it was there
                        if existing_failed:
                            failed_items.remove(existing_failed)
                            save_failed_log(failed_items)
                        
                        session_success += 1
                        continue
                    except PlaywrightTimeoutError:
                        failure_reason = "AA Download stream timeout / 404 Link"

                # ------------------------------------------------
                # Option 2 - if there is a Libgen.li direct mirror
                # ------------------------------------------------
                elif libgen_link_locator:
                    print("  [*] Standard mirror not found. Found Libgen.li link!")
                    
                    #goes to the libgen link, waits for it to load
                    href = libgen_link_locator.get_attribute("href")
                    if href.startswith("/"):
                        parsed = urlparse(page.url)
                        href = f"{parsed.scheme}://{parsed.netloc}{href}"
                        
                    print(f"  [*] Navigating directly to: {href}")
                    page.goto(href, wait_until="domcontentloaded", timeout=60000)
                    
                    print("  [*] Waiting for Libgen GET button...")
                    get_btn = page.locator("a:has-text('GET'), a[href*='get.php']").first
                    
                    try:
                        get_btn.wait_for(state="attached", timeout=120000) #waits for the button to load / become clickable
                    except PlaywrightTimeoutError:
                        failure_reason = "Libgen GET button never appeared (Timeout)" #no captcha as i don't think there are any on libgen
                        raise Exception(failure_reason)

                    print("  [*] Attempting download from Libgen.li...")
                    try:
                        with page.expect_download(timeout=20000) as download_info: #allows to download the file -> tells browser to expect one
                            get_btn.click(force=True)
                        
                        download = download_info.value
                        original_name = download.suggested_filename #gets the AA suggested name
                        
                        #generate the file name based on the terminal argument
                        file_name = generate_custom_filename(original_name, url, NAME_FORMAT)
                        file_path = os.path.join(DOWNLOAD_DIR, file_name)
                        
                        print(f"  [*] Saving {file_name}...")
                        download.save_as(file_path)
                        print(f"  [+] Success! Saved from Libgen: {file_name}\n")
                            
                        #generate the clean title from the actual downloaded file for the log
                        clean_title = clean_downloaded_title(original_name, url)
                        mark_completed(url, clean_title)
                        completed_urls.add(url)
                        
                        existing_failed = next((f for f in failed_items if f["url"] == url), None) #removes url/file from the failed items json in case it was there
                        if existing_failed:
                            failed_items.remove(existing_failed)
                            save_failed_log(failed_items)
                            
                        session_success += 1
                        continue
                    except PlaywrightTimeoutError:
                        failure_reason = "Libgen Download stream timeout / 404 Link"

                else:
                    failure_reason = "No supported slow mirrors or Libgen links found on page" #if there's none of the two download options

            except Exception as e: #if link dead, timeout or error -> script doesn't crash
                failure_reason = f"Script exception: {str(e)}"

            #log failures <- if nothing else happened
            print(f"  [-] Failed: {failure_reason}\n")
            session_failed += 1
            existing = next((f for f in failed_items if f["url"] == url), None) #reads the failed_items set (which are in ram, after the script opened the json on line 164) that's full of structs, finds if the current failed link is already present
            if existing:
                existing["reason"] = failure_reason #updates failure reason
            else:
                failed_items.append({ #adds the whole link to the failed_items (in ram)
                    "url": url,
                    "title": page_title,
                    "reason": failure_reason
                })
            
            save_failed_log(failed_items)#writes in ram changes of the json to the file

            time.sleep(3) #3 seconds of wait before the next link

        #final verdicts
        print("\n")
        print("Finished processing queue!")
        print(f"- Total Processed: {len(targets)}")
        print(f"- Successfully Downloaded: {session_success}")
        print(f"- Failed: {session_failed}")
        if session_skipped > 0:
            print(f"- Skipped (Already Completed): {session_skipped}")
        print("\n")
        print(f"You can find the list of downloaded links in the completed.txt file, in your download directory ({DOWNLOAD_DIR}).")
        if session_failed > 0:
            print(f"You can find the list of failed links in the failed_downloads.json file, in your download directory ({DOWNLOAD_DIR}).")
            print("To fix the failed downloads, you can run this script again in 'retry mode' which will try to download all your failed links again.")
        print("\n")
        
        browser.close() #shuts down playwright

if __name__ == "__main__": #standard python, tells to execute main when launched from the terminal
    main()