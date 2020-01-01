#!/usr/bin/env python3
#domain_stats.py by Mark Baggett
#Twitter @MarkBaggett
#x="""
# GOAL is the followign records
#{seen_by_web:datetime,    Comes from local for 100M ISC for everything else
# seen_by_you: datetime    First seen by you
# seen_by_isc: Position in Top 100M OR datetime first seen by ISC
# Category: Established, NEW  
# FirstContacts: YOU, ISC, BOTH
# ISC_Other:  { }  Other alerts as provided by isc for this domain
#}
#database record
#Rank will contain ISC date for >100M records

#Cache Records ???   Just straight json answers or calculated?
#Cache is straight JSON responses.  CAN NOT CACHE anything with FIRSTCONTACT
# 
# Delete expired records from the database

import http.server
import socketserver 
import expiring_cache
import database_io
import network_io
import collections
import sys
import datetime
import threading
import time
import urllib
import re
import json
import sqlite3
import config

import functools
import resource
import pathlib
import code
import logging


def dateconverter(o):
    if isinstance(o, datetime.datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")


def health_check():
    #Contacts isc returns
    #   Messages to pass on to client  
    global health_thread    
    log.debug("Submit Health Check")
    critical, interval, messages = network_io.health_check(software_version, database_version, cache, database.stats)
    if critical:
        if messages[0] == 'UPDATE-DATABASE':
            database.update_database(messages[1], config)
    critical, interval, messages = network_io.health_check(software_version, database_version, cache, database.stats)
    if critical:
        stop_msg = "Domain Stats will not start as a result of a critical error.\n"
        stop_msg += "Please resolve the following error(s):\n"
        stop_msg += "\n".join(messages)
        print(stop_msg)
        log.debug(stop_msg)
    elif not ready_to_exit.is_set():
        health_thread = threading.Timer(interval * 60, health_check)
        health_thread.start()
        return health_thread
    else:
        return None

def retrieve_server_config():   
    log.info("Retrieve server config")
    submit_data = {"action":"config","database_version":database.version,"software_version":software_version}
    resp_dict = None
    try:
        submit_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        submit_socket.settimeout(15)
        submit_socket.sendto(json.dumps(submit_data,default=dateconverter).encode(), (config['server_name'],config['server_port']))
        resp, addr = submit_socket.recvfrom(32768)
        log.info(f"Server Provided Config {resp}")
        resp_dict = json.loads(resp)
    except Exception as e:
        log.debug(f"Error retrieving server config {str(e)}")
    return resp_dict

def reduce_domain(domain_in):
    parts =  domain_in.strip().split(".")
    if len(parts)> 2: 
        if parts[-1] not in ['com','org','net','gov','edu']:
            if parts[-2] in ['co', 'com','ne','net','or','org','go','gov','ed','edu','ac','ad','gr','lg','mus','gouv']:
                domain = ".".join(parts[-3:])
            else:
                domain = ".".join(parts[-2:])
        else:
            domain = ".".join(parts[-2:])
            #print("trim top part", domain_in, domain)
    else:
        domain = ".".join(parts)
    log.debug(f"Trimmed domain from {domain_in} to {domain.lower()}")
    return domain.lower()

def load_config():
    with open("domain_stats.yaml") as fh:
        yaml_dict = yaml.safe_load(fh.read())
    Configuration = collections.namedtuple("Configuration", list(yaml_dict) )
    return Configuration(**yaml_dict)

def json_response(web,isc,you,cat,alert):
    return json.dumps({"seen_by_web":web,"seen_by_isc":isc, "seen_by_you":you, "category":cat, "alerts":alert},default=dateconverter).encode()

def domain_stats(domain):
    global cache
    log.debug(f"New Request for domain {domain}.  Here is the cache info: {cache.keys()} {cache.cache_info()}")
    #First try to get it from the Memory Cache
    domain = reduce_domain(domain)
    log.debug(f"Is the domain in cache?  {domain in cache}")
    if domain in cache:
        cache_data =  cache.get(domain)
        #Could still be None as expiration is only determined upon get()
        if cache_data:
            return cache_data
    #If it isn't in the memory cache check the database
    else:
        #import pdb;pdb.set_trace()
        record_seen_by_web, record_expires, record_seen_by_isc, record_seen_by_you = database.get_record(domain)
        if record_seen_by_web:
            #Found it in the database. Calculate categories and alerts then cache it 
            category = "NEW"
            alerts = []
            #if not expires and its doesn't expire for two years then its established.
            if record_seen_by_web < (datetime.datetime.utcnow() - datetime.timedelta(days=365*2)):
                category = "ESTABLISHED"
            if record_seen_by_you == "FIRST-CONTACT":
                record_seen_by_you = (datetime.datetime.utcnow()+datetime.timedelta(hours=config['timezone_offset']))
                alerts.append("YOUR-FIRST-CONTACT")
                database.update_record(domain, record_seen_by_web, record_expires, record_seen_by_isc, record_seen_by_you)     
            if alerts:
                #If there are alerts then it is a first contact so we do not cache it            
                cache_expiration = 0
            else:
                #If there are no alerts we cache it for 30 days (720 hours) or domain expiration (which ever comes first)
                until_expires = datetime.datetime.utcnow() - record_expires
                cache_expiration = min( 720 , (until_expires.seconds//360))
            resp = json_response(record_seen_by_web, record_seen_by_isc, record_seen_by_you,category,alerts)
            cache_resp = json_response(record_seen_by_web, record_seen_by_isc, record_seen_by_you,category,[])
            cache.set(domain,cache_resp, hours_to_live=cache_expiration)
            log.debug(f"New Cache Entry! {cache.keys()} , {cache.cache_info()}")
            return resp
        else:
            #Your here so its not in the database look to the isc?
            #if the ISC responds with an error put that in the cache
            alerts = ["YOUR-FIRST-CONTACT"]
            isc_seen_by_you = (datetime.datetime.utcnow()+datetime.timedelta(hours=config['timezone_offset']))
            isc_seen_by_web, isc_expires, isc_seen_by_isc, isc_alerts = network_io.retrieve_isc(domain)
            #handle code if the ISC RETURNS AN ERROR HERE
            #Handle it.  Cache the error for some period of time.
            #If it isn't an error then its a new entry for the database (only) no cache
            if isc_seen_by_web == "ERROR":
                cache_expiration = isc_seen_by_isc
                resp = json_response("ERROR","ERROR","ERROR","ERROR",isc_alerts)
                cache.set(domain, resp, hours_to_live=cache_expiration)
                return resp
            #here the isc returned a good record for the domain. Put it in the database and calculate an uncached response
            category = "NEW"
            #if not expires and its doesn't expire for two years then its established.
            if isc_seen_by_web < (datetime.datetime.utcnow() - datetime.timedelta(days=365*2)):
                category = "ESTABLISHED"
            alerts.extend(isc_alerts)
            resp = json_response(isc_seen_by_web, isc_seen_by_isc, isc_seen_by_you, category, alerts )
            #Build a response just for the cache that stores ISC alerts for 24 hours. 
            if "YOUR-FIRST-CONTACT" in alerts:
                alerts.remove("YOUR-FIRST-CONTACT")
            if "ISC-FIRST-CONTACT" in alerts:
                alerts.remove("ISC-FIRST-CONTACT")
            if alerts:
               cache_expiration = 24     #Alerts are only cached for 24 hours
            else:
               until_expires = datetime.datetime.utcnow() - isc_expires
               cache_expiration = min( 720 , (until_expires.seconds//360))
            cache_response = json_response(isc_seen_by_web, isc_seen_by_isc, isc_seen_by_you, category, alerts )
            cache.set(domain, cache_response, cache_expiration)
            database.update_record(domain, isc_seen_by_web, isc_expires, isc_seen_by_isc, datetime.datetime.utcnow())
            return resp


class domain_api(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type','text/plain')
        self.end_headers()
        (ignore, ignore, urlpath, urlparams, ignore) = urllib.parse.urlsplit(self.path)
        if re.search("[\/][\w.]*", urlpath):
            domain = re.search(r"[\/](.*)$", urlpath).group(1)
            #log.debug(domain)
            if domain == "stats":
                result = str(cache.cache_info()).encode() + b"\n"
                result += str(database.stats).encode()
            elif domain == "showcache":
                result = str(cache.cache_report()).encode()
            else:
                domain = reduce_domain(domain)
                result = domain_stats(domain) 
            self.wfile.write(result)
        else:
            api_hlp = 'API Documentation\nhttp://%s:%s/domain.tld   where domain is a non-dotted domain and tld is a valid top level domain.' % (self.server.server_address[0], self.server.server_address[1])
            self.wfile.write(api_hlp.encode())
        return

    def log_message(self, format, *args):
        return


class ThreadedDomainStats(socketserver.ThreadingMixIn, http.server.HTTPServer):
    def __init__(self, *args,**kwargs):
        self.args = ""
        self.screen_lock = threading.Lock()
        self.exitthread = threading.Event()
        self.exitthread.clear()
        http.server.HTTPServer.__init__(self, *args, **kwargs)

config = config.config("domain_stats.yaml")
cache = expiring_cache.ExpiringCache()
database = database_io.DomainStatsDatabase(config['database_file'])

log = logging.getLogger(__name__)
logfile = logging.FileHandler('domain_stats.log')
logformat = logging.Formatter('%(asctime)s : %(levelname)s : %(name)s : %(message)s')
logfile.setFormatter(logformat)
if config['log_detail'] == 0:
    log.setLevel(level=logging.CRITICAL)
elif config['log_detail'] == 1:
    log.addHandler(logfile)
    log.setLevel(logging.INFO)
else:
    log.addHandler(logfile)
    log.setLevel(logging.DEBUG)

software_version = 0.1
database_version = database.version


if __name__ == "__main__":
    #Reload memory cache
    cache_file = pathlib.Path(config['memory_cache'])
    if cache_file.exists():
        cache.cache_load(str(cache_file))    

    #Setup the server.
    start_time = datetime.datetime.utcnow()
    resolved_local = resolved_remote = resolved_error = resolved_db  = 0
    database_lock = threading.Lock()
    server = ThreadedDomainStats((config['local_address'], config['local_port']), domain_api)

    #Get the central server config
    prohibited_domains = config['prohibited_tlds']
    server_config = None

    #start the server
    print('Server is Ready. http://%s:%s/domain.tld' % (config['local_address'], config['local_port']))
    ready_to_exit = threading.Event()
    ready_to_exit.clear()

    health_thread = health_check()
    if not health_thread:
        sys.exit(1)
        
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True

    try:
        server_thread.start()
        code.interact(local=locals())
        #while True: time.sleep(100)
    except (KeyboardInterrupt, SystemExit):
        server.shutdown()
        server.server_close()
        
    print("Web API Disabled...")
    print("Control-C hit: Exiting server.  Please wait..")
    health_thread.cancel()
    print("Commiting Cache to disk...")
    cache.cache_dump(config['memory_cache'])

    print("Bye!")


