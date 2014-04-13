import logging
l = logging.getLogger("puppeteer.manipulator")

import abc
import struct
import string # pylint: disable=deprecated-module
import itertools

from .errors import NotLeetEnough
from .formatter import absolute_string, offset_read
from .architectures import x86
from .rop import ROPChain

# pylint: disable=no-self-use,unused-argument
class Manipulator:
    __metaclass__ = abc.ABCMeta

    def __init__(self, arch=x86):
        '''
        This should connect to or spawn up the program in question.
        '''
        self.arch = arch
        self.plt = { }
        self.rop_cleanups = { }
        self.locations = { }
        self.info = { }

        self.got_base = 0
        self.got_size = 0
        self.got_names = [ ]

    #
    # Utility funcs
    #

    def fix_endness_strided(self, s):
        '''
        Goes through the string, in chunks of the bitwidth of the architecture,
        and fixes endness.
        '''
        if self.arch.endness == '>':
            return s

        return "".join([ s[i:i+self.arch.bytes][::-1] for i in range(0, len(s), self.arch.bytes) ])



    def pack(self, n):
        if type(n) in (int, long):
            return struct.pack(self.arch.struct_fmt, n)
        if type(n) == str:
            return n

    def unpack(self, n):
        if type(n) in (int, long):
            return n
        if type(n) == str:
            return struct.unpack(self.arch.struct_fmt, n)[0]

    def _get_vulns(self, t, safe):
        vulns = [ ]
        unsafe_vulns = [ ]

        l.debug("Looking for a %s vuln with safe=%s", t, safe)

        for a in dir(self):
            #l.debug("... checking attribute %s", a)
            f = getattr(self, a)
            if hasattr(f, 'puppeteer_flags') and f.puppeteer_flags['type'] == t:
                if f.puppeteer_flags['safe']:
                    vulns.append(f)
                else:
                    unsafe_vulns.append(f)

        if safe is None:
            return vulns + unsafe_vulns
        elif safe:
            return vulns
        elif not safe:
            return unsafe_vulns

    def _do_vuln(self, vuln_type, args, kwargs, safe=None):
        funcs = self._get_vulns(vuln_type, safe)

        for f in funcs:
            try:
                l.debug("Trying function %s", f.func_name)
                return f(*args, **kwargs)
            except NotLeetEnough:
                l.debug("... failed!")

        l.debug("Couldn't find an appropriate vuln :-(")
        raise NotLeetEnough("No %s%s functions available!" % (('safe ' if safe is True else ('unsafe ' if self is False else '')), vuln_type))

    #
    # Actions!
    #

    def do_memory_read(self, addr, length, safe=None):
        ''' Finds and executes an vuln that does a memory read. '''

        # if safe is None, try safe first, then unsafe
        if safe is None:
            try: return self.do_memory_read(addr, length, safe=True)
            except NotLeetEnough: return self.do_memory_read(addr, length, safe=False)

        # first, try to do it directly
        try:
            funcs = self._get_vulns('memory_read', safe)

            for f in funcs:
                l.debug("Trying a direct memory read with %s", f.__name__)
                max_size = f.puppeteer_flags['max_size']

                r = ""
                while len(r) < length:
                    toread = min(length, max_size)
                    l.debug("... reading %d bytes", toread)
                    r += f(addr + len(r), toread)
                return r
        except NotLeetEnough:
            l.debug("... l4m3!")

        # now do the printf path
        return self.do_printf_read(addr, length, safe=safe)

    def do_register_read(self, reg, safe=None):
        ''' Finds and executes an vuln that does a register read. '''
        return self._do_vuln('register_read', (reg,), { }, safe=True)

    def do_memory_write(self, addr, content, safe=None):
        ''' Finds and executes an vuln that does a memory write. '''

        # if safe is None, try safe first, then unsafe
        if safe is None:
            try: return self.do_memory_write(addr, content, safe=True)
            except NotLeetEnough: return self.do_memory_write(addr, content, safe=False)

        l.debug("First trying a direct memory write.")
        try:
            return self._do_vuln('memory_write', (addr, content), { }, safe=True)
        except NotLeetEnough:
            l.debug("... just can't do it, captain!")

        l.debug("Now trying a naive printf write.")
        return self.do_printf_write((addr, content), safe=None)

    def do_register_write(self, reg, content, safe=None):
        ''' Finds and executes an vuln that does a register write. '''
        return self._do_vuln('register_write', (reg, content), { }, safe=True)

    def do_printf(self, fmt, safe=None):
        '''
        Finds and executes an vuln that does a memory read.

        @param fmt: the format string!
        @param safe: safety!
        '''
        return self._do_vuln('printf', (fmt,), { }, safe=None)

    def do_printf_read(self, addr, length, safe=None):
        '''
        Do a printf-based memory read.

        @param addr: the address
        @param safe: safety
        '''

        funcs = self._get_vulns('printf', safe)

        l.debug("Reading %d bytes from 0x%x using printf", length, addr)

        content = ""
        while len(content) < length:
            cur_addr = addr + len(content)
            left_length = length - len(content)

            for f in funcs:
                fmt = absolute_string(f.puppeteer_flags['bytes_to_fmt'], reads=(cur_addr,), read_length=left_length, pad_to=f.puppeteer_flags['max_fmt_size'], pad_with="_", word_size=self.arch.bytes, endness=self.arch.endness)
                try:
                    l.debug("... trying format string %s through %s.", fmt, f.func_name)
                    # FIXME: this might be cutting off to much. Do it intelligently
                    new_content = f(fmt)[self.arch.bytes:-fmt.count("_")]
                    content += new_content
                    l.debug("... got: %s (length %d)", new_content, len(new_content))
                    if f.puppeteer_flags['max_output_size'] is not None:
                        l.debug("... expected %d bytes", min(f.puppeteer_flags['max_output_size'], left_length))

                    if len(new_content) < left_length:
                        if not f.puppeteer_flags['max_output_size'] or len(new_content) < f.puppeteer_flags['max_output_size']:
                            l.debug("... skipping null byte")
                            # probably was null-terminated
                            content += '\0'
                    break
                except NotLeetEnough:
                    l.debug("... failed")

        return content

    def do_printf_write(self, writes, safe=None):
        '''
        Do a memory write using a printf vulnerability.

        @param writes: a tuple of (addr, bytes) tuples
        @param safe: whether it's ok for the program to stop functioning afterwards
        '''

        # this is an overwrite of a set of bytes. We don't care about the output.
        funcs = self._get_vulns('printf', safe)
        chunks = [ (writes[0]+i, j) for i,j in enumerate(writes[1]) ]

        for c in chunks:
            for f in funcs:
                fmt = absolute_string(f.puppeteer_flags['bytes_to_fmt'], writes=(c,), pad_to=f.puppeteer_flags['max_fmt_size'], word_size=self.arch.bytes, endness=self.arch.endness)
                try:
                    l.debug("Trying format string through %s.", f.func_name)
                    f(fmt)
                    break
                except NotLeetEnough:
                    l.debug("... failed")

        return ""

    #
    # More complex stuff
    #

    def read_got_entry(self, which, safe=None):
        if type(which) == str:
            which = self.got_names.index(which)
        return self.do_memory_read(self.got_base+which*self.arch.bytes, self.arch.bytes, safe=safe)

    def dump_got(self, which, safe=None):
        return self.do_memory_read(self.got_base, self.got_size*self.arch.bytes, safe=safe)

    def do_relative_read(self, offset, length, reg=None):
        try:
            reg = self.arch.sp_name if reg is None else reg
            return self.do_memory_read(self.do_register_read(reg) + offset, length)
        except NotLeetEnough:
            if reg != self.arch.sp_name:
                raise

            funcs = self._get_vulns('printf', None)
            for f in funcs:
                result = ""
                max_i = (length + self.arch.bytes - 1) / self.arch.bytes
                while len(result) < length:
                    fmt = offset_read(offset/self.arch.bytes + len(result)/(self.arch.bytes*2), self.arch.bytes*2, max_length=f.puppeteer_flags['max_fmt_size'], max_offset=max_i, round_to=self.arch.bytes, pad_with='_')
                    result += f(fmt).replace('_', '')

            return self.fix_endness_strided(result.decode('hex'))

    def do_page_read(self, addr):
        base = addr - (addr % self.arch.page_size)
        return self.do_memory_read(base, self.arch.page_size)

    def redirect_library_function(self, name, target, safe=None):
        '''
        Redirects a PLT entry to jump to target.

        @params name: the name to redirect
        @params target: the address to redirect to
        '''
        self.do_memory_write(self.plt[name], self.pack(target), safe=safe)

    def read_stack(self, length, safe=None):
        '''
        Read the stack, from the current stack pointer (or something close), to sp+length

        @params length: the number of bytes to read. More bytes might be attempted if we end up using
                        a printf
        @params safe: if True, only do a safe read, if False, only do an unsafe read, if None do either
        '''

        # if safe is None, try safe first, then unsafe
        if safe is None:
            try: return self.read_stack(length, safe=True)
            except NotLeetEnough: return self.read_stack(length, safe=False)

        return self.do_relative_read(0, length, reg=self.arch.sp_name)

    def main_return_address(self, start_offset=1):
        # strategy:
        # 1. search for a return address to main
        # 2. look for main's return address (to __libc_start_main)
        # 3. awesome!

        l.debug("Looking for libc!")

        for i in itertools.count(start=start_offset):
            l.debug("... checking offset %d", i)
            fmt = offset_read(i, self.arch.bytes*2, max_offset=10000, round_to=self.arch.bytes, pad_with='_')
            v = struct.unpack(">" + self.arch.struct_char, self.do_printf(fmt).replace('_', '').decode('hex'))[0]
            if v >= self.locations['main'] and v <= self.locations['#main_end']:
                l.debug("... found the return address to main (specifically, to 0x%x) at offset %d!", v, i)
                break

        i += 3 + self.info['main_stackframe_size'] / self.arch.bytes # pylint: disable=undefined-loop-variable
        l.debug("... the return address into __libc_start_main should be at offset %d", i)

        fmt = offset_read(i, self.arch.bytes*2, max_offset=10000, round_to=self.arch.bytes, pad_with='_')
        v = struct.unpack(">" + self.arch.struct_char, self.do_printf(fmt).replace('_', '').decode('hex'))[0]
        return v

    #
    # Crazy UI
    #
    def memory_display_page(self, p, addr):
        perline = 24
        print ""
        print "# Displaying the page at 0x" + (self.arch.python_fmt % addr)
        print ""
        for i in range(0, len(p), perline):
            line = p[i:i+perline]
            count = 0
            for c in line:
                print c.encode('hex'),
                count += 1
                if count % 4 == 0:
                    print "",

            print '|',"".join([ (c if c in string.letters + string.digits + string.punctuation else '.') for c in line ])

        nums = sorted(tuple(set(struct.unpack(self.arch.endness + str(self.arch.page_size/self.arch.bytes) + self.arch.struct_char, p))))

        perline = 10
        print ""
        print "# Aligned integers in the page:"
        print ""
        for i in range(0, len(nums), perline):
            line = nums[i:i+perline]
            print " ".join([ self.arch.python_fmt % c for c in line ])

        nums = sorted(tuple(set([ i - i%self.arch.page_size for i in struct.unpack(self.arch.endness + str(self.arch.page_size/self.arch.bytes) + self.arch.struct_char, p) ])))

        perline = 10
        print ""
        print "# Possible pages to look at next:"
        print ""
        for i in range(0, len(nums), perline):
            line = nums[i:i+perline]
            print " ".join([ self.arch.python_fmt % c for c in line ])

    def memory_explorer(self):
        print "###"
        print "### Super Memory Explorer 64"
        print "###"
        print ""
        sp = self.do_register_read('esp')
        print "SP:", hex(sp)

        a = 'asdf'
        addr = None

        while a != 'q':
            print ""
            print "# Please enter one of:"
            print "#"
            print "#    - sp (to go back to the stack)"
            print "#    - a hex address (to look at its page)"
            print "#    - q (to quit)"
            print "#    - '' or 'n'(to look at the next page)"
            print "#    - 'p' (to look at the previous page)"
            a = raw_input("> ")

            if a in ['sp']:
                addr = sp
            elif a in ['', 'n']:
                addr = addr + self.arch.page_size if addr is not None else sp
            elif a in ['p']:
                addr = addr - self.arch.page_size if addr is not None else sp
            else:
                try:
                    addr = int(a, 16)
                except ValueError:
                    continue

            addr -= addr % self.arch.page_size

            p = self.do_page_read(addr)
            self.memory_display_page(p, addr)



    #
    # ROP stuff
    #

    def new_rop(self, chain=None, length=None):
        return ROPChain(arch=self.arch, chain=chain, length=length)

    def rop_site_array(self, call_target, ret_target, args, cleanup=True):
        '''
        Returns a callsite for a piece of a ROP chain. Args and the ret_target
        can be None, in which case they'll either not be written (when doing an
        array write) or will be replaced with 0x41414141* (when doing a string
        write).
        '''
        s = [ ]

        # this is what we're calling
        s.append(self.pack(call_target))

        # this is where we'll return to immediately
        if cleanup:
            s.append(self.pack(self.rop_cleanups[len(args)]))
        else:
            s.append(self.pack(ret_target))

        # our arguments
        for a in args:
            if a is not None:
                s.append(self.pack(a))
            else:
                s.append(None)

        # if we're cleaning up, *now* we return to the ret target
        if cleanup and ret_target is not None:
            s.append(self.pack(ret_target))

        return s

    def rop_site_bytes(self, *args, **kwargs):
        '''
        Returns a string of a callsite for a piece of a ROP chain.
        '''
        s = ""
        for w in self.rop_site_array(*args, **kwargs):
            if w is not None:
                s += w
            else:
                s += 'A' * self.arch.bytes
        return s
