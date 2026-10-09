"""Narrow OS component definitions; metadata and embedded hashes grant no trust."""
from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET

MAIN = 'Microsoft.Windows.Common-Controls'
RESOURCE = MAIN + '.Resources'
TOKEN = '6595b64144ccf1df'
ASM = 'urn:schemas-microsoft-com:asm.v1'
V2 = 'urn:schemas-microsoft-com:asm.v2'
V3 = 'urn:schemas-microsoft-com:asm.v3'
DS = 'http://www.w3.org/2000/09/xmldsig#'


@dataclass(frozen=True)
class ComponentIdentity:
    name: str
    token: str
    architecture: str
    version: str
    language: str
    encoded_language: str | None


@dataclass(frozen=True)
class ComponentDefinition:
    identity: ComponentIdentity
    file_name: str
    optional_resource: bool


def component_identity(name, fields):
    required = {'processorArchitecture', 'publicKeyToken', 'type', 'version'}
    if name not in (MAIN, RESOURCE) or set(fields) not in (required, required | {'language'}):
        raise ValueError()
    if (fields['processorArchitecture'] != 'amd64' or fields['publicKeyToken'] != TOKEN or
            fields['type'] != 'win32'):
        raise ValueError()
    version = fields['version']
    if not re.fullmatch(r'6\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})', version):
        raise ValueError()
    if version == '6.0.0.0' or max(map(int, version.split('.'))) > 65535:
        raise ValueError()
    language = fields.get('language')
    if language is None:
        if name != MAIN: raise ValueError()
    elif not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', language):
        raise ValueError()
    # Preserve the raw API/definition spelling for exact proof; only the
    # semantic directory key is lowercase. No locale fallback is inferred here.
    return ComponentIdentity(name, TOKEN, 'amd64', version, language.lower() if language else 'neutral', language)


def encoded_identity(value):
    if type(value) is not str or not 0 < len(value) <= 2048:
        raise ValueError()
    name, *parts = value.split(',')
    fields = {}
    for part in parts:
        match = re.fullmatch(r'\s*(processorArchitecture|publicKeyToken|type|version|language)="([A-Za-z0-9.\-]+)"', part)
        if not match or match[1] in fields: raise ValueError()
        fields[match[1]] = match[2]
    return component_identity(name, fields)


def inspect_component_manifest(payload: bytes):
    if type(payload) is not bytes or not 0 < len(payload) <= 65536:
        raise ValueError()
    text = payload.decode('utf-8-sig')
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I) or '\0' in text or 'XInclude' in text:
        raise ValueError()
    if '<?' in re.sub(r'^\s*<\?xml\s[^?]*\?>', '', text, count=1):
        raise ValueError()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ValueError() from None
    q = lambda tag: '{' + ASM + '}' + tag
    if root.tag != q('assembly') or root.attrib.get('manifestVersion') != '1.0':
        raise ValueError()
    # Servicing, hash and membership fields are opaque annotations. They do not
    # resolve build macros, fetch URIs or stand in for raw independently read bytes.
    allowed = {
        q('assembly'): {'manifestVersion', 'copyright', '{' + V3 + '}copyright'},
        q('noInheritable'): set(),
        q('assemblyIdentity'): {'name', 'version', 'processorArchitecture', 'publicKeyToken', 'type', 'language'},
        q('file'): {'name', '{' + V3 + '}importPath', '{' + V3 + '}sourceName'},
        q('windowClass'): {'versioned'},
        q('dependency'): {'optional', '{' + V3 + '}discoverable'},
        q('dependentAssembly'): set(),
        '{' + V3 + '}signatureInfo': set(),
        '{' + V3 + '}signatureDescriptor': {'PETrust', 'pageHash'},
        '{' + V2 + '}hash': set(),
        '{' + DS + '}Transforms': set(),
        '{' + DS + '}Transform': {'Algorithm'},
        '{' + DS + '}DigestMethod': {'Algorithm'},
        '{' + DS + '}DigestValue': set(),
    }
    for namespace in (ASM, V3):
        allowed['{' + namespace + '}memberships'] = set()
        allowed['{' + namespace + '}categoryMembership'] = set()
        allowed['{' + namespace + '}id'] = {'buildType', 'language', 'name', 'processorArchitecture',
                                         'publicKeyToken', 'version', 'typeName'}
    # Enforce where execution-bearing fields may occur, not just known tag names.
    parents = {
        q('assembly'): {q('noInheritable'), q('assemblyIdentity'), q('file'), q('dependency'),
                        '{' + ASM + '}memberships', '{' + V3 + '}memberships'},
        q('file'): {q('windowClass'), '{' + V3 + '}signatureInfo', '{' + V2 + '}hash'},
        q('dependency'): {q('dependentAssembly')},
        q('dependentAssembly'): {q('assemblyIdentity')},
        '{' + V3 + '}signatureInfo': {'{' + V3 + '}signatureDescriptor'},
        '{' + V2 + '}hash': {'{' + DS + '}Transforms', '{' + DS + '}DigestMethod', '{' + DS + '}DigestValue'},
        '{' + DS + '}Transforms': {'{' + DS + '}Transform'},
    }
    for namespace in (ASM, V3):
        parents['{' + namespace + '}memberships'] = {'{' + namespace + '}categoryMembership'}
        parents['{' + namespace + '}categoryMembership'] = {'{' + namespace + '}id'}
    seen = 0

    def check(node, depth):
        nonlocal seen
        seen += 1
        if (depth > 8 or seen > 512 or node.tag not in allowed or
                set(node.attrib) - allowed[node.tag] or len(node.attrib) > 8 or
                any(len(k) > 256 or len(v) > 2048 for k, v in node.attrib.items())):
            raise ValueError()
        if node.tag == q('windowClass') and node.attrib.get('versioned', 'yes') not in ('yes', 'no'):
            raise ValueError()
        for child in node:
            if child.tag not in parents.get(node.tag, set()): raise ValueError()
            check(child, depth + 1)
    check(root, 0)
    identities, files = root.findall(q('assemblyIdentity')), root.findall(q('file'))
    if len(identities) != 1 or len(files) != 1 or len(root.findall(q('noInheritable'))) > 1:
        raise ValueError()
    attributes = dict(identities[0].attrib)
    name = attributes.pop('name', None)
    identity = component_identity(name, attributes)
    expected_file = 'comctl32.dll' if name == MAIN else 'comctl32.dll.mui'
    if files[0].attrib.get('name') != expected_file:
        raise ValueError()
    dependencies = root.findall(q('dependency'))
    optional = False
    if dependencies:
        if name != MAIN or len(dependencies) != 1: raise ValueError()
        dep = dependencies[0]
        if dep.attrib.get('optional') != 'yes' or dep.attrib.get('{' + V3 + '}discoverable', 'no') != 'no':
            raise ValueError()
        if len(dep) != 1 or len(dep[0]) != 1: raise ValueError()
        if dep[0][0].attrib != dict(name=RESOURCE, version='6.0.0.0', processorArchitecture='amd64',
                                   language='*', publicKeyToken=TOKEN, type='win32'):
            raise ValueError()
        optional = True
    if name == RESOURCE and files[0].find(q('windowClass')) is not None:
        raise ValueError()
    return ComponentDefinition(identity, expected_file, optional)
