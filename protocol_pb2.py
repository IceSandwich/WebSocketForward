# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# NO CHECKED-IN PROTOBUF GENCODE
# source: protocol.proto
# Protobuf Python Version: 5.29.4
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import runtime_version as _runtime_version
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder
_runtime_version.ValidateProtobufRuntimeVersion(
    _runtime_version.Domain.PUBLIC,
    5,
    29,
    4,
    '',
    'protocol.proto'
)
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()




DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n\x0eprotocol.proto\x12\tWSForward\"h\n\x07Request\x12\x0b\n\x03url\x18\x01 \x01(\t\x12\x0e\n\x06method\x18\x02 \x01(\t\x12\x14\n\x0cheaders_json\x18\x03 \x01(\t\x12\x0e\n\x06seq_id\x18\x04 \x01(\t\x12\x11\n\x04\x62ody\x18\x05 \x01(\x0cH\x00\x88\x01\x01\x42\x07\n\x05_body\"\x83\x01\n\x08Response\x12\x0b\n\x03url\x18\x01 \x01(\t\x12\x0e\n\x06status\x18\x02 \x01(\x05\x12\x14\n\x0cheaders_json\x18\x03 \x01(\t\x12\x0e\n\x06seq_id\x18\x04 \x01(\t\x12\x0c\n\x04\x62ody\x18\x05 \x01(\x0c\x12\x12\n\nsse_ticket\x18\x06 \x01(\x08\x12\x12\n\nstream_end\x18\x07 \x01(\x08\x62\x06proto3')

_globals = globals()
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, _globals)
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'protocol_pb2', _globals)
if not _descriptor._USE_C_DESCRIPTORS:
  DESCRIPTOR._loaded_options = None
  _globals['_REQUEST']._serialized_start=29
  _globals['_REQUEST']._serialized_end=133
  _globals['_RESPONSE']._serialized_start=136
  _globals['_RESPONSE']._serialized_end=267
# @@protoc_insertion_point(module_scope)
