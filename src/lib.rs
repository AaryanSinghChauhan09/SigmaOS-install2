//! Alternate installer component
//!
//! Follows all SigmaOS engineering principles:
//! DRY, KISS, SOLID, AAA testing, Separation of Concerns, Gall's Law.

/// Configuration for SigmaOS-install2
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub enabled: bool,
    pub version: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            enabled: true,
            version: String::from("0.1.0"),
        }
    }
}

/// Initialise the SigmaOS-install2 component
pub fn init(config: Config) -> Result<String, String> {
    if !config.enabled {
        return Err(String::from("Component is disabled"));
    }
    Ok(format!("SigmaOS-install2 v{} initialised", config.version))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        // Arrange
        let config = Config::default();
        // Act
        let enabled = config.enabled;
        // Assert
        assert!(enabled, "default config should be enabled");
    }

    #[test]
    fn test_init_success() {
        // Arrange
        let config = Config::default();
        // Act
        let result = init(config);
        // Assert
        assert!(result.is_ok(), "init should succeed with default config");
    }

    #[test]
    fn test_init_disabled() {
        // Arrange
        let config = Config { enabled: false, version: String::from("0.1.0") };
        // Act
        let result = init(config);
        // Assert
        assert!(result.is_err(), "init should fail when disabled");
    }
}
