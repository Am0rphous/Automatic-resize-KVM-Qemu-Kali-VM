# Λutomatic resize a KVM/QEMU Kali-VM

**!!** To make a VM automatically resize when changing the size of the window, use Video VGA instead of e.g. Virtio. Also it might be a good idea to give it more vram than only 16 mb. Use `sudo virsh edit <my-vm-name>` and adjust `vram` to 65536 / ~64 mb.

<br>

**Background:** I have several Kali VMs running KVM/Qemu with "Auto resize VM with window" enabled, but no way in hell it would just work. Every time i resized the window black edges appeared. But running `xrandr --output Virtual-1 --auto` within the VM would actually automatically resize the window beautifully. Only problem is i don't have patience doing that every time i resize the window.

<br>

I'm therefore introducing this big ass python script that runs the above command within my VM of choise. Now it fixed my problem so I'm not going to prettify it or make it universal meaning it will work for any Qemu VM running Linux.

<br>

How does it work?
1. Connects to the X11 display and finds the VM window by name/class.
2. Subscribes to StructureNotify events and blocks on d.next_event().
3. On ConfigureNotify it compares the new width/height to the last known geometry.
4. If changed, starts a debounce timer (DELAY_SECONDS) to coalesce rapid resizes.
5. When timer fires it calls virsh qemu-agent guest-exec to run xrandr inside the Kali guest.
6. Polls guest-exec-status until the guest process finishes.
7. Decodes base64 stdout/stderr from the guest and logs output, errors and exit info.
