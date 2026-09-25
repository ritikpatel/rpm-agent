-- Week 3: ICU bedside monitors speak HL7, not MQTT; an ICU gateway (IoT thing) publishes on their behalf.
-- gateway_id names the only thing allowed to publish for a device other than the device itself.
ALTER TABLE device_assignments ADD COLUMN IF NOT EXISTS gateway_id text;
CREATE INDEX IF NOT EXISTS device_assignments_gateway ON device_assignments (gateway_id) WHERE active_to IS NULL;
