#!/usr/bin/env python

from __future__ import with_statement
from string import Template
import re, fnmatch, os

VERSION = "0.10.0"

TEST_FUNC_REGEX = r"^(void\s+(test_%s__(\w+))\(\s*void\s*\))\s*\{"

EVENT_CB_REGEX = re.compile(
    r"^(void\s+clay_on_(\w+)\(\s*void\s*\))\s*\{",
    re.MULTILINE)

SKIP_COMMENTS_REGEX = re.compile(
    r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
    re.DOTALL | re.MULTILINE)

CLAY_HEADER = """
/*
 * Clay v%s
 *
 * This is an autogenerated file. Do not modify.
 * To add new unit tests or suites, regenerate the whole
 * file with `./clay`
 */
""" % VERSION

CLAY_EVENTS = [
    'init',
    'shutdown',
    'test',
    'suite'
]

def main():
    from optparse import OptionParser

    parser = OptionParser()

    parser.add_option('-c', '--clay-path', dest='clay_path')
    parser.add_option('-v', '--report-to', dest='print_mode', default='default')

    options, args = parser.parse_args()

    for folder in args or ['.']:
        builder = ClayTestBuilder(folder,
            clay_path = options.clay_path,
            print_mode = options.print_mode)

        builder.render()


class ClayTestBuilder:
    def __init__(self, path, clay_path = None, print_mode = 'default'):
        self.declarations = []
        self.suite_names = []
        self.callback_data = {}
        self.suite_data = {}
        self.event_callbacks = []

        self.clay_path = os.path.abspath(clay_path) if clay_path else None

        self.path = os.path.abspath(path)
        self.modules = [
            "clay_sandbox.c",
            "clay_fixtures.c",
            "clay_fs.c"
        ]

        self.modules.append("clay_print_%s.c" % print_mode)

        print("Loading test suites...")

        for root, dirs, files in os.walk(self.path):
            module_root = root[len(self.path):]
            module_root = [c for c in module_root.split(os.sep) if c]

            tests_in_module = fnmatch.filter(files, "*.c")

            for test_file in tests_in_module:
                full_path = os.path.join(root, test_file)
                test_name = "_".join(module_root + [test_file[:-2]])

                with open(full_path) as f:
                    self._process_test_file(test_name, f.read())

        if not self.suite_data:
            raise RuntimeError(
                'No tests found under "%s"' % folder_name)

    def render(self):
        main_file = os.path.join(self.path, 'clay_main.c')
        with open(main_file, "w") as out:
            out.write(self._render_main())

        header_file = os.path.join(self.path, 'clay.h')
        with open(header_file, "w") as out:
            out.write(self._render_header())

        print ('Written Clay suite to "%s"' % self.path)

    #####################################################
    # Internal methods
    #####################################################

    def _render_cb(self, cb):
        return '{"%s", &%s}' % (cb['short_name'], cb['symbol'])

    def _render_suite(self, suite):
        template = Template(
r"""
    {
        "${clean_name}",
        ${initialize},
        ${cleanup},
        ${cb_ptr}, ${cb_count}
    }
""")

        callbacks = {}
        for cb in ['initialize', 'cleanup']:
            callbacks[cb] = (self._render_cb(suite[cb])
                if suite[cb] else "{NULL, NULL}")

        return template.substitute(
            clean_name = suite['name'].replace("_", "::"),
            initialize = callbacks['initialize'],
            cleanup = callbacks['cleanup'],
            cb_ptr = "_clay_cb_%s" % suite['name'],
            cb_count = suite['cb_count']
        ).strip()

    def _render_callbacks(self, suite_name, callbacks):
        template = Template(
r"""
static const struct clay_func _clay_cb_${suite_name}[] = {
    ${callbacks}
};
""")
        callbacks = [
            self._render_cb(cb)
            for cb in callbacks
            if cb['short_name'] not in ('initialize', 'cleanup')
        ]

        return template.substitute(
            suite_name = suite_name,
            callbacks = ",\n\t".join(callbacks)
        ).strip()

    def _render_event_overrides(self):
        overrides = []
        for event in CLAY_EVENTS:
            if event in self.event_callbacks:
                continue

            overrides.append(
                "#define clay_on_%s() /* nop */" % event
            )

        return '\n'.join(overrides)

    def _render_header(self):
        template = Template(self._load_file('clay.h'))

        declarations = "\n".join(
            "extern %s;" % decl
            for decl in sorted(self.declarations)
        )

        return template.substitute(
            extern_declarations = declarations,
        )

    def _render_main(self):
        template = Template(self._load_file('clay.c'))
        suite_names = sorted(self.suite_names)

        suite_data = [
            self._render_suite(self.suite_data[s])
            for s in suite_names
        ]

        callbacks = [
            self._render_callbacks(s, self.callback_data[s])
            for s in suite_names
        ]

        callback_count = sum(
            len(cbs) for cbs in self.callback_data.values()
        )

        return template.substitute(
            clay_modules = self._get_modules(),
            clay_callbacks = "\n".join(callbacks),
            clay_suites = ",\n\t".join(suite_data),
            clay_suite_count = len(suite_data),
            clay_callback_count = callback_count,
            clay_event_overrides = self._render_event_overrides(),
        )

    def _load_file(self, filename):
        if self.clay_path:
            filename = os.path.join(self.clay_path, filename)
            with open(filename) as cfile:
                return cfile.read()

        else:
            import zlib, base64, sys
            content = CLAY_FILES[filename]

            if sys.version_info >= (3, 0):
                content = bytearray(content, 'utf_8')
                content = base64.b64decode(content)
                content = zlib.decompress(content)
                return str(content)
            else:
                content = base64.b64decode(content)
                return zlib.decompress(content)

    def _get_modules(self):
        return "\n".join(self._load_file(f) for f in self.modules)

    def _skip_comments(self, text):
        def _replacer(match):
            s = match.group(0)
            return "" if s.startswith('/') else s

        return re.sub(SKIP_COMMENTS_REGEX, _replacer, text)

    def _process_test_file(self, suite_name, contents):
        contents = self._skip_comments(contents)

        self._process_events(contents)
        self._process_declarations(suite_name, contents)

    def _process_events(self, contents):
        for (decl, event) in EVENT_CB_REGEX.findall(contents):
            if event not in CLAY_EVENTS:
                continue

            self.declarations.append(decl)
            self.event_callbacks.append(event)

    def _process_declarations(self, suite_name, contents):
        callbacks = []
        initialize = cleanup = None

        regex_string = TEST_FUNC_REGEX % suite_name
        regex = re.compile(regex_string, re.MULTILINE)

        for (declaration, symbol, short_name) in regex.findall(contents):
            data = {
                "short_name" : short_name,
                "declaration" : declaration,
                "symbol" : symbol
            }

            if short_name == 'initialize':
                initialize = data
            elif short_name == 'cleanup':
                cleanup = data
            else:
                callbacks.append(data)

        if not callbacks:
            return

        tests_in_suite = len(callbacks)

        suite = {
            "name" : suite_name,
            "initialize" : initialize,
            "cleanup" : cleanup,
            "cb_count" : tests_in_suite
        }

        if initialize:
            self.declarations.append(initialize['declaration'])

        if cleanup:
            self.declarations.append(cleanup['declaration'])

        self.declarations += [
            callback['declaration']
            for callback in callbacks
        ]

        callbacks.sort(key=lambda x: x['short_name'])
        self.callback_data[suite_name] = callbacks
        self.suite_data[suite_name] = suite
        self.suite_names.append(suite_name)

        print("  %s (%d tests)" % (suite_name, tests_in_suite))



CLAY_FILES = {
"clay.c" : r"""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""",
"clay_print_default.c" : r"""eJyFU01P4zAQPSe/YqgU1a5Cuafa3RunistqT4AiEztgKbUje9LVCvHfsccpOGhbTs48z3t+85HSo0DdwdFqCd0g/rWj0wZbbTSy8AGoPLadnQzWEGM/aVQnoLPGI3QvwsEmXRhxUJ6Xr2XBoiT/pO/KgqR7ttpbIZWESiY130DlH8yqhvgiX7yQq2YKv1E4VDKQAvpWlmeq8C8TSvvXfF9JBJRz1iXgXAUJypgfWEbelZ9GH0zyWJArp0brsKVczy5apxzybabDqdMe3dRhSqME2NBBdk9PQmgsh1uhh8mphvoaJHjuqvJNU3lgledwH4JKPsL9NYYjppdFQarXP6nQLI69iOHKWJDKd06PqO2C0ushZwzahPFNhyflvujM6MIXnBZhzktNPfhnytI9sPkiexyufsDdn/2eB/lzOlk6X07n8v5YE52yfM2T9bCPaWeyShLQh74r+XV/ImG3RIiTrXTVBb+JDb9gfbuGBtbb9Tf+aELs//8hmbjZgLF2hM3NcnuTo0vS4ins6kI6DKKG7XZLwkfRDjpcCfc87ij08adkMa4hzaw49nN5HmWYBeE1UXjiKCPZHL6V7yZUhjs=""",
"clay_print_tap.c" : r"""eJyNVMFu2zAMPVtfwbgIYBu2gWK3BtuwnYthh+02wFBtORXmSIYkZyiG/vso2m6lJG12skk9ko+PlJh13MkWjlp20A78qRmNVK6RSroMf8AJ65pWT8qV4G07SSdWR6uVddA+cgPFfKD4Qdic/WVJ5lPmr+G71RUAT3wrjij0Wfrjy3c4CmOlVnD74ZdK8x17ZuwNyvZxcp3+o67T9g5hjDaz43/oxr4geMdYInvINlHC5KWHGxi5taIDPgyw7YhYZnNspgxIYmOJGKyIAnsuBwzEIH7Qan8aHRQsMS6Js61pbut6251Xe1tGSksaqumwjtg6M7VuhhEACvoE0iHaa7HWBaiqah5Z4MOZW74XcAdb+9pE9Wnu5WD3MdwKHL90T3ekxVk2Gg3AWTbyx1DfPFyAen+M7FH0S0jvj5GDVCuyC5He36AcD8Lk63osR52wrZGj8xu9+Qjfft7fh8sCEABOCQRHeax0XdfXLodWtDrhhaV98NdwvhCzSaxnx7x+NOG11Nb6JawWYkh8WdHPkCrtQP9OUYwUP/4sTPhiYjmWEH0iZ8SozbJzNrvSAY01u/zmRDRvoCgKJOk/pGCAe78Ef0A6UQncydILTAWOvBkkHnGzH3dkYiYM8HYJy/r2Cw2Lr9GEr036FUUC/N0A7e/xFEAlfIp8zilUly3mM/sHrvXXzQ==""",
"clay_sandbox.c" : r"""eJyNVV1P20AQfLZ/xRIkYpNATItaVSkPlaBVVEoiEgQSRJaxz+SEfY7uLmkD4r931+fEHwRahBST3Zudmb0xSgeahxDOAgl+mAQrfx7o2e2x9+XTtG/bypS50DZX/jJIeOTrdJ43OWEmlDZH9+kL1362rfHk28SfgNJ42uIxOAThULkLe0q7sHMCnmtblmR6IQV4676dsT8Ynw4u8cCh0n6aRcxt9hXPThCGTKkC9dof/nThhGD79kuNc8xFlW/O9H4Rx0x2QfEn5mtImHgw1Hd5LCIWg389uPj4wbYKHKOy6F4G0g+zhdBwAsf9Ro/BZ2KJRkl1O8UeNMRqTX6NUFerC/SUf5yZz6vx2eXocvh9cH7WssF6QYlgFZM46Y0zCQ5HHK8PHL6W4/vQ6XA3h2/MxuYHpvHB2RDhUzTGMibjl2QqndJcLBhNySuv10utZgTKlCKcr5y1d1jqrp0j6MqSLOvFxl/b6u3DIAY9Y9TNZSZShrZFGVOijX4GKwjESs+4eOiClivQGSwUgx7Oh/2e/QapFtVbBa8mLVOsMasQQ1K7LFHMQP9gesLS+YhAndPr4eWpa451wcA1Lt8uExGPja7JjCtQK6VZuhGU8EeGAmpaSHy4kDIXziULdYbFd8Qdvqns8D1Z6z8PjqoBWGY8gjzSC6ECEd1nfxz6Lo8pEajk3ZtSgNp3XrtUjVcDI1FNRDhDFcgSaVYMiZUv0wpYM4XoJ08iv6BglG54VG4vFXwd8CRPTivHI2tu8p8WpW0T2fVLox7wkoOJdxZXabkYoOqbh9yyLQTDaeg3PtRFNNU/A65eZDLFpT2xnC4tejQcD24Ak/o7kBGoJFAzpvIlV6JsvYoyiShD3NwHL/Zxl+/DsholaPfam6htFtHAIGUHcDSlNy72m0H1eqdTgtE9Wl+7sgs6xLRbLmebszgGm7ZYRozSR4zJ3Ff/3E7jH4NZj0Gga1c97n32vK0HKgHHUzS4xhM9vbg6P391qDCwTFX9AucI/x8h2Nvbdue33z9CMbmqEt3qRY3eX120XBI=""",
"clay_fixtures.c" : r"""eJyFUV1LwzAUfW5+xZU9rLUVJ4ggZQ9DFAUfRCZMRglZmrBAl5Qkk03xv9v0a82U+Zabc+45595rLLGCAlXSWKBrouEccbGzW81wSew6HCIrYljicTuqJBsWoS8UmFbPobXA8npye5OlFSI+GbaglbK4YDJFKOjeMAVjdfUInUPkyFZLWu7DWiKBxtgpKN78RZETEByactlLXcBVBmdTGF+OIxQEPhrHGdRQ1zzMv5xUYN84ROLY8b1MEPeTJEdsV3tRq0wdt06tWcWVzXpS9I3QSPCccbh7nr3jh6fF/O31Hr/M5o9ouGpa4NYlPHmBVt074i/lBLy+OsWHEjkcXLAhMl+p3Wk3bjBV1VIG6TxOApgWZN8s4k8bWjAit+W/NnoTejMddI+GqW1GTOaCox8pOffr""",
"clay_fs.c" : r"""eJylVdtu20YQfSa/YkAD8TKWY8dJX6L0wXDEVqgsBhINN7UFhiGX1qIkl9hd+dLG/57ZCynJUWEkfZE0s7NnZufMGe2xsqAlpJfj6ZsT399DgzUUojhKo8npb3Mg+ud8PBlNE/hq/NP4LJ5G49n5aTKOp71zNJvFs4vx06DzPz6MZ6HvS5UplkO+zAS89EtWUd7KtM3UkuS8kcqdGE/o/+t71tYm/ArTi8lk6HuS/UNTBRVtbtRyAGzo+x4rgaQ2zMaFvucJqlaicdd8z15AHKkE/rbxIQI6+DqrKp4TF3YAJ2GH/AxwTeu8fTBRA0jtl0Xp0K+sucAsx9suzPPauX2v5AIIMxYweO9AhnBwwELAbvTFXLGFrmf/aF+X4/Uu2L++3scEjwjmitRnQ/+x7/0tZ0XXecIaBTUv6AC22i/5SuRPnQWVynAy/z3CSYg/zpPZxVkCJQLp4m2YvYqVbJHrEHU7bJgG+y7IZNBQf1HBz2nNxQN5oeEHoDnnJdlOHYa2aa18dRetmlxziI8ZOl8bCV5ruk3u3ptw9OlUnaeMquxGorOfd/OcKs2kpEKlBFuMibHUuKUCm8gbW1aoOTge4HFwyZqC30l4EgdlhmYR+J4tVVBK1q0wpnv0U4JkKmqygxTDQEdfFKcfRpNRMsKx6zgzM7oLL+c4oz9A80aSs/jjp40U6bpmA46t0vgVzZpVS7TLApg3lOwe55A6ivMqe3AKCV4GoQXZo5WkXbk4kr5c0qpK+UoRW5SrMBM3t1cLg60HV19YSS0nVuA+wE/dY/zSg8XF32StX/S9h2OrobIVeLskUhVUCM2eF8wfpKI1oM3FO/hsb3+GHDeCo/DVdRNozjx6zxQ5fB06lXXwehIsPr2n+S0xtR4vBqboLvguYwqD9YUBvLD1D/DesFfr5ejPcTJPTpOLObHn/4PLnkprmpJ+WQy3pbpeqNZOcenovvVCxm1ZIK0bEl4Hrpdpf2pbYs2rjchDs+f6nfVfAXYRuu6hGRx9Yc1R3gZD5zVBweGsd5wsNjVuXG+0y81O6KRuDt4u+r8Ro/B6JRWOo5RG5OuxM6QZYUeGfVAcdM9B6b3lRlpqr8ya4gu/363wZ0W9oekNjt4udvVA1N/1oNxuQvfiHc342TdbTYNa0u2XPiN9I/NV464Qs/e1a8PxiLJvClb63wD3Q6FA""",
"clay.h" : r"""eJy9Vctu2zAQPEdfwVo9WIIQp9c0DWAENmLACIrWQdsTQZOriKhMqiTVqCj67yUp+aGH46YHn0wtdzizu0M65KlgkCKM75bTb3g1+7zC9xgHoQ1yAb14EHJB85IButGG5Xx9md0GwU/JGaI5+YUx0RqUGQcXXBhEpWDccCmS4MKutY1kRKE45TkkdUpuWTq7oJRUnRgDTRUvmrMcUGeyzBkma6lM9H6nAWswmOZARFmMfWwcN59R/R1HCaoXsiA/SrDgLTbVLag7NuSp64/vwnzxdfX4aYY/Tlf3waE6B+WVKRWM22X6GBZk02JpwpoItpbVayBbdS9AQrA9T4NgEscBitHUz8O2DW0IVVKjZ24yBFWRc8oN8r1GG9CaPIHNn+wmb1k3pTa4sBPFYwtQCXJTiNqD9jsRuv2ArhLrlvliOcPYrZaLB78azUtBvQBK8hylxM6eXaMRCvdnJuhd1CN2maeJb47yzqoCqAGG0pYAI72GEwpqktP0b47XbfmV7asj5hoJaZBRJQzxbmd1lwH9/h9zog53pkFdRX3mM09qSMIZBnUVnbhUQv7jdWokDd2wh8flcvgqdECHPe+BmtJ3iLab6/TjpjtVx95ue4a+BXui9l7pwl6sxad0EYOVzKWizkT2NPseTp6JElw8ddV7AQM+OeaOFdiXtr4Ml6Phx6Jhes2pX2oIYqVyP8aRQAW0dK66Hg14zuvYgMkks5uWRBGXq319b39DZUAJfLjzJ9j+GfwFGCyeSg=="""
}
if __name__ == '__main__':
    main()
