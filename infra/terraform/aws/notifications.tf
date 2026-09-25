# SES sender identity for escalation emails. AWS sends a verification link to this address; in the SES sandbox
# every recipient must be verified too (request production access for real use). SMS uses SNS directly.
resource "aws_sesv2_email_identity" "sender" {
  count          = var.alert_sender_email == "" ? 0 : 1
  email_identity = var.alert_sender_email
}
