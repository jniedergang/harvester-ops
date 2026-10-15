#!/bin/sh
# Synthetic fixture for the rule generator, not a vendored dracut file.
{
    for p in $(getargs ifname=); do
        parse_ifname_opts "$p"
        printf 'SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*", ATTR{address}=="%s", ATTR{type}=="1", NAME="%s"\n' "$ifname_mac" "$ifname_if"
    done
} >> /etc/udev/rules.d/80-ifname.rules
