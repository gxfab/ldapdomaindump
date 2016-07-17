####################
#
# Copyright (c) 2016 Dirk-jan Mollema
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
####################

import sys, os, re, codecs, json, argparse, getpass
# import class and constants
from datetime import datetime
from urllib import quote_plus

import ldap3
from ldap3 import Server, Connection, SIMPLE, SYNC, ALL, SASL, NTLM
from ldap3.core.exceptions import *
from ldap3.abstract import attribute, attrDef
from ldap3.utils import dn

#dnspython, for resolving hostnames
import dns.resolver


#User account control flags
#From: https://blogs.technet.microsoft.com/askpfeplat/2014/01/15/understanding-the-useraccountcontrol-attribute-in-active-directory/
uac_flags = {'ACCOUNT_DISABLED':0x00000002,
        'ACCOUNT_LOCKED':0x00000010,
        'PASSWD_NOTREQD':0x00000020,
        'PASSWD_CANT_CHANGE': 0x00000040,
        'NORMAL_ACCOUNT': 0x00000200,
        'WORKSTATION_ACCOUNT':0x00001000,
        'SERVER_TRUST_ACCOUNT': 0x00002000,
        'DONT_EXPIRE_PASSWD': 0x00010000,
        'SMARTCARD_REQUIRED': 0x00040000,
        'PASSWORD_EXPIRED': 0x00800000
        }

#Password policy flags
pwd_flags = {'PASSWORD_COMPLEX':0x01,
            'PASSWORD_NO_ANON_CHANGE': 0x02,
            'PASSWORD_NO_CLEAR_CHANGE': 0x04,
            'LOCKOUT_ADMINS': 0x08,
            'PASSWORD_STORE_CLEARTEXT': 0x10,
            'REFUSE_PASSWORD_CHANGE': 0x20}

#Common attribute pretty translations
attr_translations = {'sAMAccountName':'SAM Name',
                    'cn':'CN','operatingSystem':'Operating System',
                    'operatingSystemServicePack':'Service Pack',
                    'operatingSystemVersion':'OS Version',
                    'userAccountControl':'Flags',
                    'objectSid':'SID',
                    'memberOf':'Member of groups',
                    'dNSHostName':'DNS Hostname',
                    'whenCreated':'Created on',
                    'whenChanged':'Changed on',
                    'IPv4':'IPv4 Address',
                    'lockOutObservationWindow':'Lockout time window',
                    'lockoutDuration':'Lockout Duration',
                    'lockoutThreshold':'Lockout Threshold',
                    'maxPwdAge':'Max password age',
                    'minPwdAge':'Min password age',
                    'minPwdLength':'Min password length'}

#Class containing the default config
class domainDumpConfig():
    def __init__(self):
        #Base path
        self.basepath = '.'

        #Output files basenames
        self.groupsfile = 'domain_groups' #Groups
        self.usersfile = 'domain_users' #User accounts
        self.computersfile = 'domain_computers' #Computer accounts
        self.policyfile = 'domain_policy' #General domain attributes

        #Combined files basenames
        self.users_by_group = 'domain_users_by_group' #Users sorted by group
        self.computers_by_os = 'domain_computers_by_os' #Computers sorted by OS

        #Output formats
        self.outputhtml = True
        self.outputjson = True
        self.outputgrep = True

        #Default field delimiter for greppable format is a tab
        self.grepsplitchar = '\t'

        #Other settings
        self.lookuphostnames = False #Look up hostnames of computers to get their IP address
        self.dnsserver = '' #Addres of the DNS server to use, if not specified default DNS will be used

#Domaindumper main class
class domainDumper():
    def __init__(self,server,connection,config,root=None):
        self.server = server
        self.connection = connection
        self.config = config
        #Unless the root is specified we get it from the server
        if root is None:
            self.root = self.getRoot()
        else:
            self.root = root
        self.users = None #Domain users
        self.groups = None #Domain groups
        self.computers = None #Domain computers
        self.policy = None #Domain policy
        self.groups_cnmap = None #CN map for group IDs to CN

    #Get the server root from the default naming context
    def getRoot(self):
        return self.server.info.other['defaultNamingContext'][0]

    #Query the groups of the current user
    def getCurrentUserGroups(self,username):
        self.connection.search(self.root,'(&(objectCategory=person)(objectClass=user)(sAMAccountName=%s))' % username,attributes=['memberOf'])
        try:
            return self.connection.entries[0]['memberOf']
        except LDAPKeyError:
            #No groups, probably just member of the primary group
            return []

    #Check if the user is part of the Domain Admin group
    def isDomainAdmin(self,username):
        groups = self.getCurrentUserGroups(username)
        domainsid = self.getRootSid()
        dagroup = self.getDAGroup(domainsid)
        #TODO: check if any groups of this are subgroups of (domain) administrator
        for group in groups:
            if 'CN=Administrators' in group or 'CN=Domain Admins' in group or dagroup.distinguishedName.value == group:
                return True
        return False

    #Get all users
    def getAllUsers(self):
        self.connection.extend.standard.paged_search('%s' % (self.root),'(&(objectCategory=person)(objectClass=user))',attributes=ldap3.ALL_ATTRIBUTES, paged_size=500, generator=False)
        return self.connection.entries

    #Get all computers in the domain
    def getAllComputers(self):
        self.connection.extend.standard.paged_search('CN=Computers,%s' % (self.root),'(objectClass=user)',attributes=ldap3.ALL_ATTRIBUTES, paged_size=500, generator=False)
        return self.connection.entries

    #Get all defined groups
    def getAllGroups(self):
        self.connection.extend.standard.paged_search(self.root,'(objectClass=group)',attributes=ldap3.ALL_ATTRIBUTES, paged_size=500, generator=False)
        return self.connection.entries

    #Get the domain policies (such as lockout policy)
    def getDomainPolicy(self):
        self.connection.search(self.root,'(cn=Builtin)',attributes=ldap3.ALL_ATTRIBUTES)
        return self.connection.entries

    #Get all defined security groups
    #Syntax from:
    #https://ldapwiki.willeke.com/wiki/Active%20Directory%20Group%20Related%20Searches
    def getAllSecurityGroups(self):
        self.connection.search(self.root,'(groupType:1.2.840.113556.1.4.803:=2147483648)',attributes=ldap3.ALL_ATTRIBUTES)
        return self.connection.entries

    #Get the SID of the root object
    def getRootSid(self):
        self.connection.search(self.root,'(objectClass=domain)',attributes=['objectSid'])
        try:
            sid = self.connection.entries[0].objectSid
        except LDAPAttributeError:
            return False
        except IndexError:
            return False
        return sid

    #Get Domain Admins group DN
    def getDAGroup(self,domainsid):
        self.connection.search(self.root,'(objectSid=%s-512)' % domainsid,attributes=ldap3.ALL_ATTRIBUTES)
        return self.connection.entries[0]

    #Lookup all computer DNS names to get their IP
    def lookupComputerDnsNames(self):
        ipmap = {}
        dnsresolver = dns.resolver.Resolver()
        dnsresolver.lifetime = 2
        ipdef = attrDef.AttrDef('ipv4')
        if self.config.dnsserver != '':
            dnsresolver.nameservers = [self.config.dnsserver]
        for computer in self.computers:
            try:
                answers = dnsresolver.query(computer.dNSHostName.values[0], 'A')
                ip = str(answers.response.answer[0][0])
            except dns.resolver.NXDOMAIN:
                ip = 'error.NXDOMAIN'
            except dns.resolver.Timeout:
                ip = 'error.TIMEOUT'
            except LDAPAttributeError:
                ip = 'error.NOHOSTNAME'
            #Construct a custom attribute as workaround
            ipatt = attribute.Attribute(ipdef, computer)
            ipatt.__dict__['_response'] = ip
            ipatt.__dict__['raw_values'] = [ip]
            ipatt.__dict__['values'] = [ip]
            #Add the attribute to the entry's dictionary
            computer._attributes['IPv4'] = ipatt

    #Create a dictionary of all operating systems with the computer accounts that are associated
    def sortComputersByOS(self,items):
        osdict = {}
        for computer in items:
            try:
                cos = computer.operatingSystem.value
            except LDAPAttributeError:
                cos = 'Unknown'
            try:
                osdict[cos].append(computer)
            except KeyError:
                #New OS
                osdict[cos] = [computer]
        return osdict

    #Map all groups on their ID (taken from their SID) to CNs
    #This is used for getting the primary group of a user
    def mapGroupsIdsToCns(self):
        cnmap = {}
        for group in self.groups:
            gid = int(group.objectSid.value.split('-')[-1])
            cnmap[gid] = group.cn.values[0]
        self.groups_cnmap = cnmap
        return cnmap

    #Get CN from DN
    def getGroupCnFromDn(self,dnin):
        cn = self.unescapecn(dn.parse_dn(dnin)[0][1])
        return cn

    #Unescape special DN characters from a CN (only needed if it comes from a DN)
    def unescapecn(self,cn):
        for c in ' "#+,;<=>\\\00':
            cn = cn.replace('\\'+c,c)
        return cn

    #Sort users by group they belong to
    def sortUsersByGroup(self,items):
        groupsdict = {}
        #Make sure the group CN mapping already exists
        if self.groups_cnmap is None:
            self.mapGroupsIdsToCns()
        for user in items:
            try:
                ugroups = [self.getGroupCnFromDn(group) for group in user.memberOf.values]
            #If the user is only in the default group, its memberOf property wont exist
            except LDAPAttributeError:
                ugroups = []
            #Add the user default group
            ugroups.append(self.groups_cnmap[user.primaryGroupId.value])
            for group in ugroups:
                try:
                    groupsdict[group].append(user)
                except KeyError:
                    #Group is not yet in dict
                    groupsdict[group] = [user]
        return groupsdict

    #Main function
    def domainDump(self):
        self.users = self.getAllUsers()
        self.computers = self.getAllComputers()
        self.groups = self.getAllGroups()
        if self.config.lookuphostnames:
            self.lookupComputerDnsNames()
        self.policy = self.getDomainPolicy()
        rw = reportWriter(self.config)
        rw.generateUsersReport(self)
        rw.generateGroupsReport(self)
        rw.generateComputersReport(self)
        rw.generateUsersByGroupReport(self)
        rw.generateComputersByOsReport(self)
        rw.generatePolicyReport(self)


class reportWriter():
    def __init__(self,config):
        self.config = config
        if self.config.lookuphostnames:
            self.computerattributes = ['cn','sAMAccountName','dNSHostName','IPv4','operatingSystem','operatingSystemServicePack','operatingSystemVersion','lastLogon','userAccountControl','whenCreated','objectSid','description']
        else:
            self.computerattributes = ['cn','sAMAccountName','dNSHostName','operatingSystem','operatingSystemServicePack','operatingSystemVersion','lastLogon','userAccountControl','whenCreated','objectSid','description']
        self.userattributes = ['cn','name','sAMAccountName','memberOf','whenCreated','whenChanged','lastLogon','userAccountControl','pwdLastSet','objectSid','description']
        self.groupattributes = ['cn','sAMAccountName','whenCreated','whenChanged','description','objectSid']
        self.policyattributes = ['cn','lockOutObservationWindow','lockoutDuration','lockoutThreshold','maxPwdAge','minPwdAge','minPwdLength','pwdHistoryLength','pwdProperties']

    #Escape HTML special chars
    def htmlescape(self,html):
        return (html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&#39;").replace('"', "&quot;"))

    #Unescape special DN characters from a CN (only needed if it comes from a DN)
    def unescapecn(self,cn):
        for c in ' "#+,;<=>\\\00':
            cn = cn.replace('\\'+c,c)
        return cn

    #Convert password max age (in 100 nanoseconds), to days
    def nsToDays(self,length):
        return abs(length) * .0000001 / 86400

    def nsToMinutes(self,length):
        return abs(length) * .0000001 / 60

    #Parse bitwise flags into a list
    def parseFlags(self,attribute,flags_def):
        outflags = []
        for flag, val in flags_def.items():
            if attribute.value & val:
                outflags.append(flag)
        return outflags

    #Generate a HTML table from a list of entries, with the specified attributes as column
    def generateHtmlTable(self,listable,attributes,header='',firstTable=True):
        of = []
        #Only if this is the first table it is an actual table, the others are just bodies of the first table
        #This makes sure that multiple tables have their columns aligned to make it less messy
        if firstTable:
            of.append(u'<table>')
        #Table header
        if header != '':
            of.append(u'<thead><tr><td colspan="%d" id="cn_%s">%s</td></tr></thead>' % (len(attributes),self.formatId(header),header))
        of.append(u'<tbody><tr>')
        for hdr in attributes:
            try:
                #Print alias of this attribute if there is one
                of.append(u'<th>%s</th>' % self.htmlescape(attr_translations[hdr]))
            except KeyError:
                of.append(u'<th>%s</th>' % self.htmlescape(hdr))
        of.append(u'</tr>\n')
        for li in listable:
            of.append(u'<tr>')
            for att in attributes:
                try:
                    of.append(u'<td>%s</td>' % self.formatAttribute(li[att]))
                except LDAPKeyError:
                    of.append(u'<td>&nbsp;</td>')
            of.append(u'</tr>\n')
        of.append(u'</tbody>\n')
        return u''.join(of)

    #Generate several HTML tables for grouped reports
    def generateGroupedHtmlTables(self,groups,attributes):
        ol = []
        first = True
        for osfamily, members in groups.iteritems():
            ol.append(self.generateHtmlTable(members,attributes,osfamily,first))
            if first:
                first = False
        out = ''.join(ol)
        return out

    #Write generated HTML to file
    def writeHtmlFile(self,rel_outfile,body):
        if not os.path.exists(self.config.basepath):
            os.makedirs(self.config.basepath)
        outfile = os.path.join(self.config.basepath,rel_outfile)
        with codecs.open(outfile,'w','utf8') as of:
            of.write('<!DOCTYPE html>\n<html>\n<head><meta charset="UTF-8">')
            #Include the style
            try:
                with open(os.path.join(os.path.dirname(__file__),'style.css'),'r') as sf:
                    of.write('<style type="text/css">')
                    of.write(sf.read())
                    of.write('</style>')
            except IOError:
                log_warn('style.css not found in package directory, styling will be skipped')
            of.write('</head><body>')
            of.write(body)
            #Does the body contain an open table?
            if '<table>' in body and not '</table>' in body:
                of.write('</table>')
            of.write('</body></html>')

    #Write generated JSON to file
    def writeJsonFile(self,rel_outfile,body):
        if not os.path.exists(self.config.basepath):
            os.makedirs(self.config.basepath)
        outfile = os.path.join(self.config.basepath,rel_outfile)
        with codecs.open(outfile,'w','utf8') as of:
            of.write(body)

    #Write generated Greppable stuff to file
    def writeGrepFile(self,rel_outfile,body):
        if not os.path.exists(self.config.basepath):
            os.makedirs(self.config.basepath)
        outfile = os.path.join(self.config.basepath,rel_outfile)
        with codecs.open(outfile,'w','utf8') as of:
            of.write(body)

    #Format a value for HTML
    def formatString(self,value):
        if type(value) is datetime:
            try:
                return value.strftime('%x %X')
            except ValueError:
                #Invalid date
                return u'0'
        if type(value) is unicode:
            return value#.encode('utf8')
        if type(value) is str:
            return unicode(value, errors='replace')#.encode('utf8')
        if type(value) is int:
            return unicode(value)
        #Other type: just return it
        return value

    #Format an attribute to a human readable format
    def formatAttribute(self,att):
        aname = att.key.lower()
        #User flags
        if aname == 'useraccountcontrol':
            return ', '.join(self.parseFlags(att,uac_flags))
        #List of groups
        if aname == 'member' or aname == 'memberof' and type(att.values) is list:
            return self.formatGroupsHtml(att.values)
        #Pwd flags
        if aname == 'pwdproperties':
            return ', '.join(self.parseFlags(att,pwd_flags))
        if aname == 'minpwdage' or  aname == 'maxpwdage':
            return '%.2f days' % self.nsToDays(att.value)
        if aname == 'lockoutobservationwindow' or  aname == 'lockoutduration':
            return '%.1f minutes' % self.nsToMinutes(att.value)
        #Other
        return self.htmlescape(self.formatString(att.value))

    #Convert a CN to a valid HTML id by replacing all non-ascii characters with a _
    def formatId(self,cn):
        return re.sub('[^a-zA-Z0-9_\-]+','_',cn)

    #Format groups to readable HTML
    def formatGroupsHtml(self,grouplist):
        outcache = []
        for group in grouplist:
            cn = self.unescapecn(dn.parse_dn(group)[0][1])
            outcache.append(u'<a href="%s.html#cn_%s" title="%s">%s</a>' % (self.config.users_by_group,quote_plus(self.formatId(cn)),self.htmlescape(group),self.htmlescape(cn)))
        return ', '.join(outcache)

    #Format groups to readable HTML
    def formatGroupsGrep(self,grouplist):
        outcache = []
        for group in grouplist:
            cn = self.unescapecn(dn.parse_dn(group)[0][1])
            outcache.append(cn)
        return ', '.join(outcache)

    #Format attribute for grepping
    def formatGrepAttribute(self,att):
        aname = att.key.lower()
        #User flags
        if aname == 'useraccountcontrol':
            return ', '.join(self.parseFlags(att,uac_flags))
        #List of groups
        if aname == 'member' or aname == 'memberof' and type(att.values) is list:
            return self.formatGroupsGrep(att.values)
        #Pwd flags
        if aname == 'pwdproperties':
            return ', '.join(self.parseFlags(att,pwd_flags))
        if aname == 'minpwdage' or  aname == 'maxpwdage':
            return '%.2f days' % self.nsToDays(att.value)
        if aname == 'lockoutobservationwindow' or  aname == 'lockoutduration':
            return '%.1f minutes' % self.nsToMinutes(att.value)
        return self.formatString(att.value)

    #Generate grep/awk/cut-able output
    def generateGrepList(self,entrylist,attributes):
        hdr = self.config.grepsplitchar.join(attributes)
        out = [hdr]
        for entry in entrylist:
            eo = []
            for attr in attributes:
                try:
                    eo.append(self.formatGrepAttribute(entry[attr]))
                except LDAPKeyError:
                    eo.append('')
            out.append(self.config.grepsplitchar.join(eo))
        return '\n'.join(out)

    #Convert a list of entities to a JSON string
    #String concatenation is used here since the entities have their own json generate
    #method and converting the string back to json just to process it would be inefficient
    def generateJsonList(self,entrylist):
        out = '[' + ','.join([entry.entry_to_json() for entry in entrylist]) + ']'
        return out

    #Convert a group key/value pair to json
    #Same methods as previous function are used
    def generateJsonGroup(self,group):
        out = '{%s:%s}' % (json.dumps(group[0]),self.generateJsonList(group[1]))
        return out

    #Convert a list of group dicts with entry lists to JSON string
    #Same methods as previous functions are used
    def generateJsonGroupedList(self,groups):
        grouplist = ','.join([self.generateJsonGroup(group) for group in groups.iteritems()])
        return '[' + grouplist + ']'

    #Generate report of all computers grouped by OS family
    def generateComputersByOsReport(self,dd):
        grouped = dd.sortComputersByOS(dd.computers)
        if self.config.outputhtml:
            html = self.generateGroupedHtmlTables(grouped,self.computerattributes)
            self.writeHtmlFile('%s.html' % self.config.computers_by_os,html)
        if self.config.outputjson:
            jsonout = self.generateJsonGroupedList(grouped)
            self.writeJsonFile('%s.json' % self.config.computers_by_os,jsonout)

    #Generate report of all groups and detailled user info
    def generateUsersByGroupReport(self,dd):
        grouped = dd.sortUsersByGroup(dd.users)
        if self.config.outputhtml:
            html = self.generateGroupedHtmlTables(grouped,self.userattributes)
            self.writeHtmlFile('%s.html' % self.config.users_by_group,html)
        if self.config.outputjson:
            jsonout = self.generateJsonGroupedList(grouped)
            self.writeJsonFile('%s.json' % self.config.users_by_group,jsonout)

    #Generate report with just a table of all users
    def generateUsersReport(self,dd):
        if self.config.outputhtml:
            html = self.generateHtmlTable(dd.users,self.userattributes,'Domain users')
            self.writeHtmlFile('%s.html' % self.config.usersfile,html)
        if self.config.outputjson:
            jsonout = self.generateJsonList(dd.users)
            self.writeJsonFile('%s.json' % self.config.usersfile,jsonout)
        if self.config.outputgrep:
            grepout = self.generateGrepList(dd.users,self.userattributes)
            self.writeGrepFile('%s.grep' % self.config.usersfile,grepout)

    #Generate report with just a table of all computer accounts
    def generateComputersReport(self,dd):
        if self.config.outputhtml:
            html = self.generateHtmlTable(dd.computers,self.computerattributes,'Domain computer accounts')
            self.writeHtmlFile('%s.html' % self.config.computersfile,html)
        if self.config.outputjson:
            jsonout = self.generateJsonList(dd.computers)
            self.writeJsonFile('%s.json' % self.config.computersfile,jsonout)
        if self.config.outputgrep:
            grepout = self.generateGrepList(dd.computers,self.computerattributes)
            self.writeGrepFile('%s.grep' % self.config.computersfile,grepout)

    #Generate report with just a table of all computer accounts
    def generateGroupsReport(self,dd):
        if self.config.outputhtml:
            html = self.generateHtmlTable(dd.groups,self.groupattributes,'Domain groups')
            self.writeHtmlFile('%s.html' % self.config.groupsfile,html)
        if self.config.outputjson:
            jsonout = self.generateJsonList(dd.groups)
            self.writeJsonFile('%s.json' % self.config.groupsfile,jsonout)
        if self.config.outputgrep:
            grepout = self.generateGrepList(dd.groups,self.groupattributes)
            self.writeGrepFile('%s.grep' % self.config.groupsfile,grepout)

    #Generate policy report
    def generatePolicyReport(self,dd):
        if self.config.outputhtml:
            html = self.generateHtmlTable(dd.policy,self.policyattributes,'Domain policy')
            self.writeHtmlFile('%s.html' % self.config.policyfile,html)
        if self.config.outputjson:
            jsonout = self.generateJsonList(dd.policy)
            self.writeJsonFile('%s.json' % self.config.policyfile,jsonout)
        if self.config.outputgrep:
            grepout = self.generateGrepList(dd.policy,self.policyattributes)
            self.writeGrepFile('%s.grep' % self.config.policyfile,grepout)

#Some quick logging helpers
def log_warn(text):
    print '[!] %s' % text
def log_info(text):
    print '[*] %s' % text
def log_success(text):
    print '[+] %s' % text

def main():
    parser = argparse.ArgumentParser(description='Domain information dumper via LDAP. Dumps users/computers/groups and OS/membership information to HTML/JSON/greppable output.')
    parser._optionals.title = "Main options"
    parser._positionals.title = "Required options"

    #Main parameters
    #maingroup = parser.add_argument_group("Main options")
    parser.add_argument("host", type=str,metavar='HOSTNAME',help="Hostname/ip or ldap://host:port connection string to connect to")
    parser.add_argument("-u","--user",type=str,metavar='USERNAME',help="DOMAIN\username for authentication, leave empty for anonymous authentication")
    parser.add_argument("-p","--password",type=str,metavar='PASSWORD',help="Password or LM:NTLM hash, will prompt if not specified")

    #Output parameters
    outputgroup = parser.add_argument_group("Output options")
    outputgroup.add_argument("-o","--outdir",type=str,metavar='DIRECTORY',help="Directory in which the dump will be saved (default: current)")
    outputgroup.add_argument("--no-html", action='store_true',help="Disable HTML output")
    outputgroup.add_argument("--no-json", action='store_true',help="Disable JSON output")
    outputgroup.add_argument("--no-grep", action='store_true',help="Disable Greppable output")
    outputgroup.add_argument("-d","--delimiter",help="Field delimiter for greppable output (default: tab)")

    #Additional options
    miscgroup = parser.add_argument_group("Misc options")
    miscgroup.add_argument("-r", "--resolve", action='store_true',help="Resolve computer hostnames (might take a while and cause high traffic on large networks)")
    miscgroup.add_argument("-n", "--dns-server",help="Use custom DNS resolver instead of system DNS (try a domain controller IP)")

    args = parser.parse_args()
    #Create default config
    cnf = domainDumpConfig()
    #Dns lookups?
    if args.resolve:
        cnf.lookuphostnames = True
    #Custom dns server?
    if args.dns_server is not None:
        cnf.dnsserver = args.dns_server
    #Custom separator?
    if args.delimiter is not None:
        cnf.grepsplitchar = args.delimiter
    #Disable html?
    if args.no_html:
        cnf.outputhtml = False
    #Disable json?
    if args.no_json:
        cnf.outputjson = False
    #Disable grep?
    if args.no_grep:
        cnf.outputgrep = False
    #Custom outdir?
    if args.outdir is not None:
        cnf.basepath = args.outdir
    #Prompt for password if not set
    authentication = None
    if args.user is not None:
        authentication = NTLM
        if not '\\' in args.user:
            log_warn('Username must include a domain, use: DOMAIN\username')
            sys.exit(1)
        if args.password is None:
            args.password = getpass.getpass()
    else:
        log_info('Connecting as anonymous user, dumping will probably fail. Consider specifying a username/password to login with')
    # define the server and the connection
    s = Server(args.host, get_info=ALL)
    log_info('Connecting to host...')
    c = Connection(s, user=args.user, password=args.password, authentication=authentication)
    log_info('Binding to host')
    # perform the Bind operation
    if not c.bind():
        log_warn('Could not bind with specified credentials')
        log_warn(c.result)
        sys.exit(1)
    log_success('Bind OK')
    log_info('Starting domain dump')
    #Create domaindumper object
    dd = domainDumper(s,c,cnf)

    #Do the actual dumping
    dd.domainDump()
    log_success('Domain dump finished')

if __name__ == '__main__':
    main()