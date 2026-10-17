# XDG
set -gx XDG_CONFIG_HOME $HOME/.config
set -gx XDG_DATA_HOME $HOME/.local/share
set -gx XDG_CACHE_HOME $HOME/.cache
set -gx XDG_STATE_HOME $HOME/.local/state

# Languages
set -gx GOPATH $HOME/langs/go
set -gx CARGO_HOME $HOME/langs/.cargo
set -gx RUSTUP_HOME $HOME/langs/.rustup
set -gx NPM_CONFIG_USERCONFIG $HOME/langs/npm/config
set -gx NPM_CONFIG_CACHE $XDG_CACHE_HOME/npm
set -gx NUGET_PACKAGES $HOME/langs/nuget/packages

# Lain-lain
set -gx GNUPGHOME $XDG_DATA_HOME/gnupg
set -gx PARALLEL_HOME $XDG_CONFIG_HOME/parallel
set -gx LESSHISTFILE $XDG_STATE_HOME/less/history
set -gx WGETRC $XDG_CONFIG_HOME/wgetrc

# PATH
fish_add_path $HOME/.local/bin $GOPATH/bin $CARGO_HOME/bin
