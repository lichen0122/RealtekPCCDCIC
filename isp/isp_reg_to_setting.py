import argparse
import csv
import logging
import re
import traceback
from io import StringIO
from pathlib import Path

try:
    import colorlog
except ImportError:
    colorlog = None


def setup_logger():
    """Set up and return a colored logger if colorlog is installed,
    otherwise a plain stderr logger."""
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)

    if colorlog is not None:
        formatter = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            reset=True,
            log_colors={
                'DEBUG':    'cyan',
                'INFO':     'green',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'red,bg_white',
            },
            secondary_log_colors={},
            style='%'
        )
        handler = colorlog.StreamHandler()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


log = setup_logger()


def _parse_op_args(body: str):
    """Parse the comma-separated arguments inside an OP(...) call,
    respecting double-quoted strings (which may contain commas)."""
    reader = csv.reader(StringIO(body), skipinitialspace=True)
    return [c.strip() for c in next(reader)]


def parse_reg_h(path_h: Path):
    """Parse a *_reg.h file and return a list of field dicts.

    Each OP() row describes one (possibly array) field. After the
    'a, b, c, d, e' placeholders, the trailing args are fixed:

        sign, array_type, array_size, bit_width, min, max, default, (trail)

    Preceding those are 1 or 2 name args, depending on the IP convention:
        - Two names (e.g. ive_reg.h):  FIELD_UPPER, field_lower
        - One name (e.g. r2yl_reg.h):  field_lower (uppercase derived)

    The description is the trailing /* ... */ comment. Default is a
    single value for scalars, or a quoted comma-list for arrays.
    """
    text = path_h.read_text(errors='replace')
    fields = []
    i = 0
    while True:
        idx = text.find('OP(', i)
        if idx == -1:
            break
        depth = 1
        j = idx + 3
        while j < len(text) and depth > 0:
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
            j += 1
        body = text[idx + 3:j - 1]
        rest = text[j:]
        m = re.match(r'\s*/\*(.*?)\*/', rest, re.DOTALL)
        desc = m.group(1).strip() if m else ''
        i = j

        try:
            row = _parse_op_args(body)
        except Exception:
            continue

        # Skip the macro signature line `OP(OP, a, b, c, d, e)` — data
        # rows always start with a literal 'a'.
        if len(row) < 13 or row[0] != 'a':
            continue

        # 5 leading placeholders (a..e) + 8 trailing fixed args, with
        # 1 or 2 name args sandwiched between.
        name_args = row[5:-8]
        tail = row[-8:]
        if len(name_args) == 2:
            name_upper, name_lower = name_args
        elif len(name_args) == 1:
            name_lower = name_args[0]
            name_upper = name_lower.upper()
        else:
            continue

        sign, array_type, array_size_s, bit_width_s, min_s, max_s, default_raw, _trail = tail
        is_array = array_type == 'A'
        array_size = int(array_size_s) if array_size_s else 0
        bit_width = int(bit_width_s)
        min_val = int(min_s)
        max_val = int(max_s)
        if default_raw.startswith('"') and default_raw.endswith('"'):
            default_raw = default_raw[1:-1]

        fields.append({
            'name_upper': name_upper,
            'name_lower': name_lower,
            'sign': sign,
            'is_array': is_array,
            'array_size': array_size,
            'bit_width': bit_width,
            'min': min_val,
            'max': max_val,
            'default_raw': default_raw,
            'description': desc,
        })

    return fields


def _two_complement_hex(val: int, bit_width: int) -> str:
    if val < 0:
        val = (1 << bit_width) + val
    return f"{val:x}"


def _field_defaults(f: dict):
    if f['is_array']:
        parts = [p.strip() for p in f['default_raw'].split(',')]
        return [int(p) for p in parts]
    return [int(f['default_raw'])]


def _is_full_range(f: dict) -> bool:
    bw = f['bit_width']
    if f['sign'] == 'U':
        return f['min'] == 0 and f['max'] == (1 << bw) - 1
    return f['min'] == -(1 << (bw - 1)) and f['max'] == (1 << (bw - 1)) - 1


def _expanded_entries(fields):
    """Yield (idx_in_field, full_upper, lower, defval, bit_width, sign, parent_field)
    for every register row in declaration order, skipping cmodel_only fields."""
    for f in fields:
        if f['description'].lstrip().startswith('cmodel_only'):
            continue
        defaults = _field_defaults(f)
        if f['is_array']:
            for i in range(f['array_size']):
                yield (
                    i,
                    f"{f['name_upper']}_{i}",
                    f"{f['name_lower']}_{i}",
                    defaults[i],
                    f['bit_width'],
                    f['sign'],
                    f,
                )
        else:
            yield (
                None,
                f['name_upper'],
                f['name_lower'],
                defaults[0],
                f['bit_width'],
                f['sign'],
                f,
            )


def gen_sv(mod_name: str, fields, path_out: Path):
    """Generate a UVM .sv setting class from parsed field info."""
    PREFIX = mod_name.upper()
    GUARD = f"GUARD_{PREFIX}_SETTING_SV"

    entries = list(_expanded_entries(fields))
    needs_xlsx = any(
        not _is_full_range(f)
        for f in fields
        if not f['description'].lstrip().startswith('cmodel_only')
    )

    out = []
    p = out.append

    p(f"`ifndef {GUARD}")
    p(f"`define {GUARD}")
    p("")
    p(f"class {mod_name}_setting extends isp_base_setting;")
    p("   //regmodel")
    p(f"   ral_block_{mod_name}_regmodel rm;")
    p(f"   //{mod_name} reg field")

    for _, upper, lower, defval, bw, sign, _ in entries:
        p(f"   //{PREFIX}_{upper}")
        signed_kw = "signed " if sign == 'S' else ""
        bracket = f"[{bw - 1}:0] " if bw > 1 else ""
        reset_hex = f"{bw}'h{_two_complement_hex(defval, bw)}"
        p(f"   rand bit {signed_kw}{bracket}{lower};   //reset value {reset_hex}")

    p("")
    p("   //constraints")
    if needs_xlsx:
        p("   constraint legal_ipu {")
        p("       soft w >= 64;")
        p("       soft w <= 3840;")
        p("       soft h >= 40;")
        p("       soft h <= 2400;")
        p("       soft frame_num >= 1;")
        p("       soft frame_num <= 3;")
        p("       soft frame_data_mode == ISP_RAND;")
        p("   };")
        p("")
        p("   constraint random_frame_data {")
        p("       soft frame_num >= 1;")
        p("       soft frame_num <= 3;")
        p("       soft frame_data_mode == ISP_RAND;")
        p("   };")
        p("")
        p("   constraint reg_constraint_by_xlsx {")
        max_name_len = max(len(lower) for _, _, lower, _, _, _, _ in entries)
        align_w = max_name_len + 2
        for _, _, lower, _, _, _, parent in entries:
            mn, mx = parent['min'], parent['max']
            padded = lower + ' ' * (align_w - len(lower))
            p(f"       {padded}inside {{[{mn}:{mx}]}};")
        p("   };")
    else:
        p("   constraint legal_ipu {")
        p("       w >= 64;")
        p("       w <= 3840;")
        p("       h >= 40;")
        p("       h <= 2400;")
        p("       frame_num >= 1;")
        p("       frame_num <= 3;")
        p("       soft frame_data_mode == ISP_RAND;")
        p("   };")
        p("")
        p("   constraint random_frame_data {")
        p("       frame_num >= 1;")
        p("       frame_num <= 3;")
        p("       soft frame_data_mode == ISP_RAND;")
        p("   };")

    p("")
    p(f"   extern function new(string name = \"{mod_name}_setting\");")
    p("   extern virtual function void class2cmodel();")
    p("   extern virtual function void reg2class();")
    p("   extern function void post_randomize();")
    p("   extern virtual function void reset();")
    p("   extern virtual task config_reg();")
    p("   extern function void update_set();")
    if needs_xlsx:
        p("   extern virtual function void do_equation();")

    p("")
    p(f"   `uvm_object_utils_begin({mod_name}_setting)")
    for _, _, lower, _, _, _, _ in entries:
        p(f"      `uvm_field_int({lower}, UVM_DEFAULT)")
    p("   `uvm_object_utils_end")
    p("")
    p(f"endclass // {mod_name}_setting")
    p("")

    p(f"function {mod_name}_setting::new(string name = \"{mod_name}_setting\");")
    p("   super.new(name);")
    p(f"   {mod_name}_cmodel_init();")
    p("endfunction // new")
    p("")

    p(f"function void {mod_name}_setting::class2cmodel();")
    p("   super.class2cmodel();")
    for _, upper, lower, _, _, _, _ in entries:
        p(f"   //{PREFIX}_{upper}")
        p(f"   cmodel_w(\"{lower}\", {lower});")
    p("endfunction // class2cmodel")
    p("")

    p(f"function void {mod_name}_setting::reg2class();")
    p("   super.reg2class();")
    for _, upper, lower, _, _, _, _ in entries:
        p(f"   //{PREFIX}_{upper}")
        p(f"   {lower} = rm.{PREFIX}_{upper}.{lower}.get();")
    p("endfunction // reg2class")
    p("")

    if needs_xlsx:
        p(f"function void {mod_name}_setting::do_equation();")
        p("endfunction")
        p("")

    p(f"function void {mod_name}_setting::post_randomize();")
    p("   super.post_randomize();")
    if needs_xlsx:
        p("   do_equation();")
    p(f"   set_{mod_name}_img_info(w, h, depth_i, ch_i, fmt_i, w, h, depth_o, ch_o, fmt_o);//set cmodel img info")
    p("   if(auto_class2cmodel) class2cmodel();")
    p("endfunction // post_randomize")
    p("")

    p(f"function void {mod_name}_setting::reset();")
    p("   super.reset();")
    for _, _, lower, defval, bw, _, _ in entries:
        p(f"   {lower} = {bw}'h{_two_complement_hex(defval, bw)};")
    p("endfunction // reset")
    p("")

    p(f"task {mod_name}_setting::config_reg();")
    p("   super.config_reg();")
    p("   if(rm == null) begin")
    p(f"      `uvm_fatal(\"{mod_name}_setting\", \"rm null\")")
    p("   end")
    for _, upper, lower, _, bw, _, _ in entries:
        pad = 32 - bw
        p(f"   rm.{PREFIX}_{upper}.write(status, {{{pad}'h0, this.{lower}}}, UVM_FRONTDOOR);")
    p("endtask // config_reg")
    p("")

    p(f"function void {mod_name}_setting::update_set();")
    p("   super.update_set();")
    p("   auto_class2cmodel = 0;")
    for _, _, lower, _, _, _, _ in entries:
        p(f"   //{lower}.rand_mode(0);")
    p("   assert(this.randomize());")
    p("endfunction //update_set")
    p("")
    p(f"`endif //{GUARD}")

    path_out.write_text('\n'.join(out) + '\n', encoding='utf-8', newline='\n')


def setting_gen(mod_name: str, path_cmod, path_spec, path_hw_sheet=None, reg_prefix="ISP"):
    """Parse a register header and emit a UVM .sv setting class.

    The hw_sheet/reg_prefix arguments are kept for CLI compatibility but
    are not consumed by the .sv emitter.
    """
    fields = parse_reg_h(Path(path_cmod))
    gen_sv(mod_name, fields, Path(path_spec))
    return False  # parse_failed = False


def main():
    parser = argparse.ArgumentParser(
        description="Generate UVM .sv setting classes from register header files.")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    single_parser = subparsers.add_parser('single', help='Process a single module')
    single_parser.add_argument('mod_name', help='Module name (e.g. ipu_ive)')
    single_parser.add_argument('path_cmod', help='Path to *_reg.h file')
    single_parser.add_argument('path_spec', help='Path to output .sv file')
    single_parser.add_argument('path_hw_sheet', nargs='?', help='Optional hardware sheet (unused for .sv output)')
    single_parser.add_argument('--reg_prefix', nargs='?', default='ISP', help='Register prefix (kept for compat)')

    batch_parser = subparsers.add_parser('batch', help='Process multiple modules using a batch directory')
    batch_parser.add_argument('ip_name', help='Name of the IP project')
    batch_parser.add_argument('path_ip_src', help='Path to the IP project (containing *_reg.h files)')
    batch_parser.add_argument('path_out', help='Path to output folder')
    batch_parser.add_argument('--reg_prefix', default='ISP', help='Register prefix (kept for compat)')
    batch_parser.add_argument('--bypass_mod', help='Comma-separated module names to skip')

    args = parser.parse_args()

    if args.command == 'single':
        kwargs = {
            'mod_name': args.mod_name,
            'path_cmod': args.path_cmod,
            'path_spec': args.path_spec,
            'reg_prefix': args.reg_prefix,
        }
        if args.path_hw_sheet is not None:
            kwargs['path_hw_sheet'] = args.path_hw_sheet
        if setting_gen(**kwargs):
            raise Exception("Parse failed")

    elif args.command == 'batch':
        out_p = Path(args.path_out)
        out_p.mkdir(exist_ok=True, parents=True)
        src_p = Path(args.path_ip_src)
        bypass = set()
        if args.bypass_mod:
            bypass = {m.strip().lower() for m in args.bypass_mod.split(',')}

        failed = []
        for reg_h in sorted(src_p.rglob('*_reg.h')):
            mod_name = reg_h.parent.name
            if mod_name.lower() in bypass:
                log.info(f"bypass {mod_name}")
                continue
            out_sv = out_p / f"{mod_name}_setting.sv"
            log.info(f"=== generating {mod_name} from {reg_h} -> {out_sv} ===")
            try:
                setting_gen(mod_name, reg_h, out_sv, reg_prefix=args.reg_prefix)
            except Exception:
                log.exception(f"{mod_name} parse failed")
                failed.append(mod_name)

        if failed:
            log.critical(f"Failed: {failed}")
        else:
            log.info(f"Success: {args.ip_name}")
        log.info(f"Output: {out_p.resolve()}")
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"Error: {e}")
        traceback.print_exc()
        exit(1)
