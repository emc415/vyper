from vyper.address_space import MEMORY
from vyper.codegen.core import _freshname, eval_once_check, make_setter
from vyper.codegen.ir_node import IRnode, push_label_to_stack
from vyper.exceptions import StateAccessViolation, StructureException
from vyper.semantics.types.subscriptable import TupleT

_label_counter = 0


# TODO a more general way of doing this
def _generate_label(name: str) -> str:
    global _label_counter
    _label_counter += 1
    return f"label{_label_counter}"


def _align_kwargs(func_t, pos_args_ir, call_expr):
    # stub
    # TODO is this the right module for me?
    """
    Using a list of args, find the internal method to use, and
    the kwargs which need to be filled in by the compiler
    """
    def _check(cond, s="Unreachable"):
        if not cond:
            raise CompilerPanic(s)

    # these should have been caught during type checking; sanity check
    _check(func_t.is_internal)
    _check(func_t.min_arg_count <= len(pos_args_ir) <= func_t.max_arg_count)
    # more sanity check, that the types match
    # _check(all(l.typ == r.typ for (l, r) in zip(args_ir, sig.args))

    num_provided_kwargs = len(args_ir) - func_t.min_arg_count
    num_kwargs = func_t.max_arg_count - func_t.min_arg_count
    kwargs_needed = num_kwargs - num_provided_kwargs

    kw_vals = list(sig.default_values.values())[:kwargs_needed]

    return sig, kw_vals

def ir_for_self_call(call_expr, context):
    from vyper.codegen.expr import Expr  # TODO rethink this circular import

    # ** Internal Call **
    # Steps:
    # - copy arguments into the soon-to-be callee
    # - allocate return buffer
    # - push jumpdest (callback ptr) and return buffer location
    # - jump to label
    # - (private function will fill return buffer and jump back)

    method_name = call_expr.func.attr
    func_t = call_expr._metadata["type"]

    print(type(func_t))
    print(call_expr)
    print(type(call_expr))
    pos_args_ir = [Expr(x, context).ir_node for x in call_expr.args]
    #TODO: after change sig to ContractFunctionT make sure to rename sig to func_t
    sig, kw_vals = context._align_kwargs(method_name, pos_args_ir, call_expr)

    kw_args_ir = [Expr(x, context).ir_node for x in kw_vals]

    args_ir = pos_args_ir + kw_args_ir

    args_tuple_t = TupleT([x.typ for x in args_ir])
    args_as_tuple = IRnode.from_list(["multi"] + [x for x in args_ir], typ=args_tuple_t)

    if context.is_constant() and func_t.is_modifying:
        raise StateAccessViolation(
            f"May not call state modifying function "
            f"'{method_name}' within {context.pp_constancy()}.",
            call_expr,
        )

    # TODO move me to type checker phase
    if not sig.internal:
        raise StructureException("Cannot call external functions via 'self'", call_expr)

    return_label = _generate_label(f"{sig.internal_function_label}_call")

    # allocate space for the return buffer
    # TODO allocate in stmt and/or expr.py
    if sig.return_type is not None:
        return_buffer = IRnode.from_list(
            context.new_internal_variable(sig.return_type), annotation=f"{return_label}_return_buf"
        )
    else:
        return_buffer = None

    # note: dst_tuple_t != args_tuple_t
    dst_tuple_t = TupleT([arg.typ for arg in sig.args])
    args_dst = IRnode(sig.frame_info.frame_start, typ=dst_tuple_t, location=MEMORY)

    # if one of the arguments is a self call, the argument
    # buffer could get borked. to prevent against that,
    # write args to a temporary buffer until all the arguments
    # are fully evaluated.
    if args_as_tuple.contains_self_call:
        copy_args = ["seq"]
        # TODO deallocate me
        tmp_args_buf = IRnode(
            context.new_internal_variable(dst_tuple_t), typ=dst_tuple_t, location=MEMORY
        )
        copy_args.append(
            # --> args evaluate here <--
            make_setter(tmp_args_buf, args_as_tuple)
        )

        copy_args.append(make_setter(args_dst, tmp_args_buf))

    else:
        copy_args = make_setter(args_dst, args_as_tuple)

    goto_op = ["goto", sig.internal_function_label]
    # pass return buffer to subroutine
    if return_buffer is not None:
        goto_op += [return_buffer]
    # pass return label to subroutine
    goto_op += [push_label_to_stack(return_label)]

    call_sequence = ["seq"]
    call_sequence.append(eval_once_check(_freshname(call_expr.node_source_code)))
    call_sequence.extend([copy_args, goto_op, ["label", return_label, ["var_list"], "pass"]])
    if return_buffer is not None:
        # push return buffer location to stack
        call_sequence += [return_buffer]

    o = IRnode.from_list(
        call_sequence,
        typ=sig.return_type,
        location=MEMORY,
        annotation=call_expr.get("node_source_code"),
        add_gas_estimate=sig.gas_estimate,
    )
    o.is_self_call = True
    return o
