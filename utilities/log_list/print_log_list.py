#!/usr/bin/env python
"""Parse and print the list of logs, after validating signature."""


import base64
import datetime
import hashlib
import json
import math
import os
import sys

import gflags
import jsonschema
import M2Crypto

FLAGS = gflags.FLAGS

gflags.DEFINE_string("log_list", None, "Logs list file to parse and print.")
gflags.MarkFlagAsRequired("log_list")
gflags.DEFINE_string("signature", None, "Signature file over the list of logs.")
gflags.DEFINE_string("signer_key", None, "Public key of the log list signer.")
gflags.DEFINE_string("log_list_schema",
                     os.path.join(os.path.dirname(sys.argv[0]),
                                  "data", "log_list_schema.json"),
                     "JSON schema for the list of logs.")
gflags.DEFINE_string("header_output", None,
                     "If specifed, generates C++ code for Chromium.")
gflags.DEFINE_boolean("skip_signature_check", False,
                     "Skip signature check (only validate schema).")


def is_log_list_valid(json_log_list, schema_file):
    try:
        jsonschema.validate(
            json_log_list,
            json.load(open(schema_file, "rb")))
        return True
    except jsonschema.exceptions.ValidationError as e:
        print e
        return False
    return False


def is_signature_valid(log_list_data, signature_file, public_key_file):
    loaded_pubkey = M2Crypto.RSA.load_pub_key(public_key_file)
    pubkey = M2Crypto.EVP.PKey()
    pubkey.assign_rsa(loaded_pubkey)
    pubkey.reset_context(md="sha256")
    pubkey.verify_init()
    pubkey.verify_update(log_list_data)
    return pubkey.verify_final(open(signature_file, "rb").read())


def print_formatted_log_list(json_log_list):
    operator_id_to_name = dict(
        [(o["id"], o["name"]) for o in json_log_list["operators"]])

    for log_info in json_log_list["logs"]:
        print "%s:" % log_info["description"]
        log_operators = [
            operator_id_to_name[i].encode("utf-8")
            for i in log_info["operated_by"]]
        print "  Operated by %s and has MMD of %f hours" % (
            ", ".join(log_operators),
            log_info["maximum_merge_delay"] / (60.0 ** 2))
        print "  At: %s" % (log_info["url"])
        key = base64.decodestring(log_info["key"])
        hasher = hashlib.sha256()
        hasher.update(key)
        key_hash = hasher.digest()
        print "  Key ID: %s" % (base64.encodestring(key_hash))


def write_cpp_header(f, include_guard):
    year = datetime.date.today().year
    f.write((
        "// Copyright %(year)d The Chromium Authors. All rights reserved.\n"
        "// Use of this source code is governed by a BSD-style license"
        " that can be\n"
        "// found in the LICENSE file.\n\n"
        "// This file is generated by print_log_list.py\n"
        "#ifndef %(include_guard)s\n"
        "#define %(include_guard)s\n\n" %
        {"year": year, "include_guard": include_guard}))


def write_log_info_struct_definition(f):
    f.write(
        "struct CTLogInfo {\n"
        "  const char* const log_key;\n"
        "  const size_t log_key_length;\n"
        "  const char* const log_name;\n"
        "  const char* const log_url;\n"
        "};\n\n")


def write_cpp_footer(f, include_guard):
    f.write("\n#endif  // %s\n" % include_guard)


def generate_code_for_chromium(json_log_list, output_file):
    """Generate a header file of known logs to be included by Chromium."""
    include_guard = (output_file.upper().replace('.', '_').replace('/', '_')
                     + '_')
    f = open(output_file, "w")
    write_cpp_header(f, include_guard)
    logs = json_log_list["logs"]
    list_code = []
    for log in logs:
        log_key = base64.decodestring(log["key"])
        hex_key = "".join(["\\x%.2x" % ord(c) for c in log_key])
        # line_width % 4 must be 0 to avoid splitting the hex-encoded key
        # across '\' which will escape the quotation marks.
        line_width = 68
        assert line_width % 4 == 0
        num_splits = int(math.ceil(len(hex_key) / float(line_width)))
        split_hex_key = ['"%s"' % hex_key[i * line_width:(i + 1) * line_width]
                         for i in range(num_splits)]
        s = "    {"
        s += "\n     ".join(split_hex_key)
        s += ',\n     %d' % (len(log_key))
        s += ',\n     "%s"' % (log["description"])
        s += ',\n     "https://%s/"}' % (log["url"])
        list_code.append(s)

    write_log_info_struct_definition(f)
    f.write("const CTLogInfo kCTLogList[] = {\n")
    f.write(",\n" . join(list_code))
    f.write("};\n")
    f.write("\nconst size_t kNumKnownCTLogs = %d;\n" % len(logs))
    write_cpp_footer(f, include_guard)


def run():
    with open(FLAGS.log_list, "rb") as f:
        json_data = f.read()

    if (not FLAGS.skip_signature_check) and not is_signature_valid(
        json_data, FLAGS.signature, FLAGS.signer_key):
        print "ERROR: Signature over list of logs is not valid, not proceeding."
        sys.exit(1)

    parsed_json = json.loads(json_data)
    if not is_log_list_valid(parsed_json, FLAGS.log_list_schema):
        print "ERROR: Log list is signed but does not conform to the schema."
        sys.exit(2)
    if FLAGS.header_output:
        generate_code_for_chromium(parsed_json, FLAGS.header_output)
    else:
        print_formatted_log_list(parsed_json)


if __name__ == "__main__":
    sys.argv = FLAGS(sys.argv)
    run()
