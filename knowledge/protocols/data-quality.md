---
id: P-DATAQ
title: Wearable data quality and device status (demo protocol)
---
## Signal quality
Each reading carries a signal-quality index from 0 to 1. Readings below 0.6 are stored but excluded from scoring; motion artefact often produces implausible heart rates of 150-180/min with low quality. Do not escalate on low-quality data alone. Request a repeat measurement instead.

## Device silent
If no data has arrived for 10 minutes, the cause may be a flat battery, the watch being off the wrist, a lost connection, or a patient who is unwell. For high-risk patients, contact the patient when the device goes silent, and treat an unexplained silence as a possible emergency when recent readings were abnormal.

## Store-and-forward
After a connectivity gap the phone gateway uploads buffered readings. Late data is scored and shown with a "late" marker. Deterioration found in late data must still be reviewed, and the delay must be taken into account.
