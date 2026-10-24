    # SigmaOS-install2

    Alternate installer (ubiquity-style)

    Part of the **SigmaOS** sovereign operating-system ecosystem — a multi-repository
    distribution modelled on Linux Mint and Omarchy Linux. See the parent
    [`SigmaOS`](https://github.com/AaryanSinghChauhan09/SigmaOS) repository for the
    full component index.

    ## Upstream inspirations
    - https://github.com/linuxmint/ubiquity

    ## Design principles applied
    - **Tests First (Plan & Validate)**
- **Pure Plan / Thin Execute**
- **Fail-Fast**
- **Defense in Depth**
- **Deterministic Testing**

    ## Usage
    Verbs: `validate-manifest, plan, install-plan, summary`

    ```
    $ sigma-installer validate-manifest install.json
$ sigma-installer plan install.json
$ sigma-installer install-plan install.json
$ sigma-installer summary install.json

    ```

    ## Build & test
    ```
    make build
    make test
    ```

    ## Licence
    GNU General Public License v3.0 (FOSS).
