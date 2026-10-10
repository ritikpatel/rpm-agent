# Clinician identity: Amazon Cognito user pool. The console signs in with the hosted UI (authorization code + PKCE,
# public client); the API verifies access tokens (signature via JWKS, issuer, client_id, token_use) and maps Cognito
# groups to roles (services/api/src/rpm_api/auth.py).

resource "aws_cognito_user_pool" "clinicians" {
  name                     = "${local.name}-clinicians"
  deletion_protection      = "INACTIVE" # demo environment is destroyed with cloud-down
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL" # TOTP; set "ON" for production
  software_token_mfa_configuration {
    enabled = true
  }
  admin_create_user_config {
    allow_admin_create_user_only = true # no self sign-up: accounts are provisioned per care-team member
    invite_message_template {
      email_subject = "Your RPM console account"
      email_message = "Username: {username} · temporary password: {####} · sign in within 3 days and set your own."
      sms_message   = "RPM console username {username}, temporary password {####}"
    }
  }
  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 5
      max_length = 254
    }
  }
}

resource "aws_cognito_user_group" "role" {
  for_each     = toset(["nurse", "physician", "admin", "presenter"]) # must match ROLES in rpm_api/auth.py
  name         = each.key
  user_pool_id = aws_cognito_user_pool.clinicians.id
  description  = "RPM console role: ${each.key}"
}

resource "aws_cognito_user_pool_domain" "console" {
  domain       = "${local.name}-${local.account_id}" # hosted UI prefix (globally unique per region)
  user_pool_id = aws_cognito_user_pool.clinicians.id
}

resource "aws_cognito_user_pool_client" "console" {
  name                                 = "${local.name}-console"
  user_pool_id                         = aws_cognito_user_pool.clinicians.id
  generate_secret                      = false # browser app: PKCE instead of a client secret
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [for u in var.console_urls : "${u}/callback"]
  logout_urls                          = var.console_urls
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60 # minutes; the console refreshes silently
  id_token_validity                    = 60
  refresh_token_validity               = 12 # hours: one clinical shift
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}
