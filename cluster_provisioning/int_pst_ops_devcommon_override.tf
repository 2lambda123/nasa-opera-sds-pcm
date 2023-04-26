# This override file is used by non-dev deployments on venues used by non-developers for
# testing, PST processing, and operations.ß

variable "hysds_release" {
  default = "v4.1.0-beta.4"
}

variable "lambda_package_release" {
  default = "2.0.0-rc.4.0"
}

variable "pcm_commons_branch" {
  default = "2.0.0-rc.4.0"
}

variable "pcm_branch" {
  default = "2.0.0-rc.4.0"
}

variable "product_delivery_branch" {
  default = "2.0.0-rc.4.0"
}

variable "bach_api_branch" {
  default = "2.0.0-rc.4.0"
}

variable "bach_ui_branch" {
  default = "2.0.0-rc.4.0"
}