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




DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n\x0eprotocol.proto\x12\tWSForward\"\xb3\x01\n\tTransport\x12\x11\n\ttimestamp\x18\x01 \x01(\x05\x12\x10\n\x08\x66rom_uid\x18\x02 \x01(\t\x12\x0e\n\x06to_uid\x18\x03 \x01(\t\x12\x0e\n\x06seq_id\x18\x04 \x01(\t\x12\x0f\n\x07\x63ur_idx\x18\x05 \x01(\x05\x12\x11\n\ttotal_cnt\x18\x06 \x01(\x05\x12/\n\tdata_type\x18\x07 \x01(\x0e\x32\x1c.WSForward.TransportDataType\x12\x0c\n\x04\x64\x61ta\x18\x08 \x01(\x0c\"X\n\x07Request\x12\x0b\n\x03url\x18\x01 \x01(\t\x12\x0e\n\x06method\x18\x02 \x01(\t\x12\x14\n\x0cheaders_json\x18\x03 \x01(\t\x12\x11\n\x04\x62ody\x18\x04 \x01(\x0cH\x00\x88\x01\x01\x42\x07\n\x05_body\"K\n\x08Response\x12\x0b\n\x03url\x18\x01 \x01(\t\x12\x0e\n\x06status\x18\x02 \x01(\x05\x12\x14\n\x0cheaders_json\x18\x03 \x01(\t\x12\x0c\n\x04\x62ody\x18\x04 \x01(\x0c\"(\n\nTCPConnect\x12\x0c\n\x04host\x18\x01 \x01(\t\x12\x0c\n\x04port\x18\x02 \x01(\x05\"I\n\x07\x43ontrol\x12-\n\tdata_type\x18\x01 \x01(\x0e\x32\x1a.WSForward.ControlDataType\x12\x0f\n\x07message\x18\x02 \x01(\t*\x84\x01\n\x11TransportDataType\x12\x0b\n\x07\x43ONTROL\x10\x00\x12\x0b\n\x07REQUEST\x10\x01\x12\x0c\n\x08RESPONSE\x10\x02\x12\x0e\n\nSUBPACKAGE\x10\x03\x12\x15\n\x11STREAM_SUBPACKAGE\x10\x04\x12\x0f\n\x0bTCP_CONNECT\x10\x05\x12\x0f\n\x0bTCP_MESSAGE\x10\x06*n\n\x0f\x43ontrolDataType\x12\r\n\tHEARTBEAT\x10\x00\x12\t\n\x05PRINT\x10\x01\x12\x08\n\x04\x45XIT\x10\x02\x12\x11\n\rQUERY_CLIENTS\x10\x03\x12\x10\n\x0cRETRIEVE_PKG\x10\x04\x12\x12\n\x0eHISTORY_CLIENT\x10\x05\x62\x06proto3')

_globals = globals()
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, _globals)
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'protocol_pb2', _globals)
if not _descriptor._USE_C_DESCRIPTORS:
  DESCRIPTOR._loaded_options = None
  _globals['_TRANSPORTDATATYPE']._serialized_start=496
  _globals['_TRANSPORTDATATYPE']._serialized_end=628
  _globals['_CONTROLDATATYPE']._serialized_start=630
  _globals['_CONTROLDATATYPE']._serialized_end=740
  _globals['_TRANSPORT']._serialized_start=30
  _globals['_TRANSPORT']._serialized_end=209
  _globals['_REQUEST']._serialized_start=211
  _globals['_REQUEST']._serialized_end=299
  _globals['_RESPONSE']._serialized_start=301
  _globals['_RESPONSE']._serialized_end=376
  _globals['_TCPCONNECT']._serialized_start=378
  _globals['_TCPCONNECT']._serialized_end=418
  _globals['_CONTROL']._serialized_start=420
  _globals['_CONTROL']._serialized_end=493
# @@protoc_insertion_point(module_scope)
