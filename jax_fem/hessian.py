import jax
import jax.numpy as np
import jax.flatten_util
import time

from jax_fem.solver import solver, linear_solver, get_flatten_fn, apply_bc, get_A
from jax_fem import logger


def tree_l2_norm_error(θ1, θ2):
    return np.sqrt(jax.tree_util.tree_reduce(lambda x, y: x + y,
        jax.tree_util.tree_map(lambda x, y: np.sum((x - y)**2), θ1, θ2)))


def forward_step(problem, θ, solver_options):
    def F_fn(u, θ):
        problem.set_params(θ)
        res_fn = problem.compute_residual
        res_fn = get_flatten_fn(res_fn, problem)
        res_fn = apply_bc(res_fn, problem)
        dofs = jax.flatten_util.ravel_pytree(u)[0]
        res_vec = res_fn(dofs)
        res_list = problem.unflatten_fn_sol_list(res_vec)
        return res_list

    # Solve forward problem
    logger.debug(f"################## Solve forward problem...")
    problem.set_params(θ)
    u = solver(problem, solver_options) # Newton's method

    return u, F_fn


def adjoint_step(problem, u, θ, J_fn, F_fn, adjoint_solver_options):
    # Solve adjoint problem
    logger.debug(f"################## Solve adjoint problem...")
    A = get_A(problem)
    λ_rhs = jax.grad(J_fn)(u, θ)
    λ_rhs_vec = jax.flatten_util.ravel_pytree(λ_rhs)[0]
    A.transpose()
    λ_vec = linear_solver(A, -λ_rhs_vec, None, adjoint_solver_options)
    λ = problem.unflatten_fn_sol_list(λ_vec)
    A.transpose() # This step is necessary because A is already changed (in-place update)
    return λ, A


def forward_and_adjoint(problem, θ, J_fn, solver_options, adjoint_solver_options):
    u, F_fn = forward_step(problem, θ, solver_options)
    λ, A = adjoint_step(problem, u, θ, J_fn, F_fn, adjoint_solver_options)
    return u, λ, F_fn, A


def incremental_forward_and_adjoint(u, θ, λ, θ_hat, J_fn, F_fn, A, state_linear_solver, adjoint_linear_solver, option='rev_fwd'):
    _, unflatten = jax.flatten_util.ravel_pytree(u)

    # Solve incremental forward problem
    logger.debug(f"################## Solve incremental forward problem...")
    u_hat_rhs = jax.jvp(lambda θ: F_fn(u, θ), (θ,), (θ_hat,))[1]
    u_hat_rhs_vec = jax.flatten_util.ravel_pytree(u_hat_rhs)[0]
    u_hat_vec = state_linear_solver(A, -u_hat_rhs_vec)
    u_hat = unflatten(u_hat_vec)

    # Solve incremental adjoint problem
    logger.debug(f"################## Solve incremental adjoint problem...")

    if option == 'fwd_rev':
        start_time = time.perf_counter()
        def grad_fn(u, θ):
            grad_u, grad_θ = jax.grad(J_fn, argnums=(0, 1))(u, θ) # (∂/∂u_k)J, (∂/∂θ_k)J
            return grad_u, grad_θ

        # Possible improvement can be made here since we only need the sum (du_k_du_j_J_u_hat_j + du_k_dθ_j_J_θ_hat_j).
        du_k_du_j_J_u_hat_j = jax.jvp(lambda u: grad_fn(u, θ)[0], (u,), (u_hat,))[1] # [(∂/∂u_k)(∂/∂u_j)J] * u_hat_j
        du_k_dθ_j_J_θ_hat_j = jax.jvp(lambda θ: grad_fn(u, θ)[0], (θ,), (θ_hat,))[1] # [(∂/∂u_k)(∂/∂θ_j)J] * θ_hat_j
        dθ_k_du_j_J_u_hat_j = jax.jvp(lambda u: grad_fn(u, θ)[1], (u,), (u_hat,))[1] # [(∂/∂θ_k)(∂/∂u_j)J] * u_hat_j
        dθ_k_dθ_j_J_θ_hat_j = jax.jvp(lambda θ: grad_fn(u, θ)[1], (θ,), (θ_hat,))[1] # [(∂/∂θ_k)(∂/∂θ_j)J] * θ_hat_j
        
        J_time = time.perf_counter() - start_time
        logger.debug(f"fwd_rev: time elapsed for J-related evaluation is {J_time}")
        start_time = time.perf_counter()

        def vjp_fn(u, θ):
            # Compute VJP of F_fn along λ at (u, θ) 
            primals_out, vjp_func = jax.vjp(F_fn, u, θ)
            vjp_u, vjp_θ = vjp_func(λ) # λ_i * (∂/∂u_k)F_i, λ_i * (∂/∂θ_k)F_i
            return vjp_u, vjp_θ

        du_k_du_j_F_i_λ_i_u_hat_j = jax.jvp(lambda u: vjp_fn(u, θ)[0], (u,), (u_hat,))[1] # [(∂/∂u_k)(∂/∂u_j)F_i] * λ_i * u_hat_j
        du_k_dθ_j_F_i_λ_i_θ_hat_j = jax.jvp(lambda θ: vjp_fn(u, θ)[0], (θ,), (θ_hat,))[1] # [(∂/∂u_k)(∂/∂θ_j)F_i] * λ_i * θ_hat_j
        dθ_k_du_j_F_i_λ_i_u_hat_j = jax.jvp(lambda u: vjp_fn(u, θ)[1], (u,), (u_hat,))[1] # [(∂/∂θ_k)(∂/∂u_j)F_i] * λ_i * u_hat_j
        dθ_k_dθ_j_F_i_λ_i_θ_hat_j = jax.jvp(lambda θ: vjp_fn(u, θ)[1], (θ,), (θ_hat,))[1] # [(∂/∂θ_k)(∂/∂θ_j)F_i] * λ_i * θ_hat_j
        
        F_time = time.perf_counter() - start_time
        logger.debug(f"fwd_rev: time elapsed for F-related evaluation is {F_time}")

    elif option == 'rev_fwd':
        start_time = time.perf_counter()
        def jvp_fn_J_u(u, θ):
            # Compute JVP of J_fn along u_hat at (u, θ) 
            jvp_u = jax.jvp(lambda u: J_fn(u, θ), (u,), (u_hat,))[1] # (∂/∂u_j)J * u_hat_j 
            return jvp_u

        def jvp_fn_J_θ(u, θ):
            # Compute JVP of J_fn along θ_hat at (u, θ) 
            jvp_θ = jax.jvp(lambda θ: J_fn(u, θ), (θ,), (θ_hat,))[1] # (∂/∂θ_j)J * θ_hat_j  
            return jvp_θ

        # [(∂/∂u_k)(∂/∂u_j)J] * u_hat_j, [(∂/∂θ_k)(∂/∂u_j)J] * u_hat_j
        du_k_du_j_J_u_hat_j, dθ_k_du_j_J_u_hat_j = jax.grad(jvp_fn_J_u, argnums=(0, 1))(u, θ)
        # [(∂/∂u_k)(∂/∂θ_j)J] * θ_hat_j, [(∂/∂θ_k)(∂/∂θ_j)J] * θ_hat_j
        du_k_dθ_j_J_θ_hat_j, dθ_k_dθ_j_J_θ_hat_j = jax.grad(jvp_fn_J_θ, argnums=(0, 1))(u, θ)

        J_time = time.perf_counter() - start_time
        logger.debug(f"rev_fwd: time elapsed for J-related evaluation is {J_time}")
        start_time = time.perf_counter()

        def jvp_fn_F_u(u, θ):
            # Compute JVP of F_fn along u_hat at (u, θ) 
            jvp_u = jax.jvp(lambda u: F_fn(u, θ), (u,), (u_hat,))[1] # (∂/∂u_j)F_i * u_hat_j 
            return jvp_u

        def jvp_fn_F_θ(u, θ):
            # Compute JVP of F_fn along θ_hat at (u, θ) 
            jvp_θ = jax.jvp(lambda θ: F_fn(u, θ), (θ,), (θ_hat,))[1] # (∂/∂θ_j)F_i * θ_hat_j  
            return jvp_θ

        primals_out_u, vjp_func_u = jax.vjp(jvp_fn_F_u, u, θ)
        # [(∂/∂u_k)(∂/∂u_j)F_i] * λ_i * u_hat_j, [(∂/∂θ_k)(∂/∂u_j)F_i] * λ_i * u_hat_j
        du_k_du_j_F_i_λ_i_u_hat_j, dθ_k_du_j_F_i_λ_i_u_hat_j = vjp_func_u(λ)
        primals_out_θ, vjp_func_θ = jax.vjp(jvp_fn_F_θ, u, θ)
        # [(∂/∂u_k)(∂/∂θ_j)F_i] * λ_i * θ_hat_j, [(∂/∂θ_k)(∂/∂θ_j)F_i] * λ_i * θ_hat_j
        du_k_dθ_j_F_i_λ_i_θ_hat_j, dθ_k_dθ_j_F_i_λ_i_θ_hat_j = vjp_func_θ(λ)

        F_time = time.perf_counter() - start_time
        logger.debug(f"rev_fwd: time elapsed for F-related evaluation is {F_time}")

    elif option == 'rev_rev':
        start_time = time.perf_counter()
        def grad_fn(u, θ):
            grad_u, grad_θ = jax.grad(J_fn, argnums=(0, 1))(u, θ) # (∂/∂u_j)J, (∂/∂θ_j)J
            return grad_u, grad_θ

        primals_out_u, vjp_func_u = jax.vjp(lambda u, θ: grad_fn(u, θ)[0], u, θ)
        # [(∂/∂u_k)(∂/∂u_j)J] * u_hat_j, [(∂/∂θ_k)(∂/∂u_j)J] * u_hat_j
        du_k_du_j_J_u_hat_j, dθ_k_du_j_J_u_hat_j = vjp_func_u(u_hat)

        primals_out_θ, vjp_func_θ = jax.vjp(lambda u, θ: grad_fn(u, θ)[1], u, θ)
        # [(∂/∂u_k)(∂/∂θ_j)J] * θ_hat_j, [(∂/∂θ_k)(∂/∂θ_j)J] * θ_hat_j
        du_k_dθ_j_J_θ_hat_j, dθ_k_dθ_j_J_θ_hat_j = vjp_func_θ(θ_hat)

        J_time = time.perf_counter() - start_time
        logger.debug(f"rev_rev: time elapsed for J-related evaluation is {J_time}")
        start_time = time.perf_counter()

        def vjp_fn(u, θ):
            # Compute VJP of F_fn along λ at (u, θ) 
            primals_out, vjp_func = jax.vjp(F_fn, u, θ)
            vjp_u, vjp_θ = vjp_func(λ) # λ_i * (∂/∂u_j)F_i, λ_i * (∂/∂θ_j)F_i
            return vjp_u, vjp_θ

        primals_out_u, vjp_func_u = jax.vjp(lambda u, θ: vjp_fn(u, θ)[0], u, θ)
        # [(∂/∂u_k)(∂/∂u_j)F_i] * λ_i * u_hat_j, [(∂/∂θ_k)(∂/∂u_j)F_i] * λ_i * u_hat_j
        du_k_du_j_F_i_λ_i_u_hat_j, dθ_k_du_j_F_i_λ_i_u_hat_j = vjp_func_u(u_hat)
        primals_out_θ, vjp_func_θ = jax.vjp(lambda u, θ: vjp_fn(u, θ)[1], u, θ)
        # [(∂/∂u_k)(∂/∂θ_j)F_i] * λ_i * θ_hat_j, [(∂/∂θ_k)(∂/∂θ_j)F_i] * λ_i * θ_hat_j
        du_k_dθ_j_F_i_λ_i_θ_hat_j, dθ_k_dθ_j_F_i_λ_i_θ_hat_j = vjp_func_θ(θ_hat)

        F_time = time.perf_counter() - start_time
        logger.debug(f"rev_rev: time elapsed for F-related evaluation is {F_time}")

    else:
        raise NotImplementedError(f"Unknown option {option}")

    λ_hat_rhs = jax.tree_util.tree_map(lambda x1, x2, x3, x4: x1 + x2 + x3 + x4,
                                       du_k_du_j_J_u_hat_j, 
                                       du_k_dθ_j_J_θ_hat_j, 
                                       du_k_du_j_F_i_λ_i_u_hat_j, 
                                       du_k_dθ_j_F_i_λ_i_θ_hat_j)

    λ_hat_rhs_vec = jax.flatten_util.ravel_pytree(λ_hat_rhs)[0]
    A.transpose()
    λ_hat_vec = adjoint_linear_solver(A, -λ_hat_rhs_vec)
    A.transpose()
    λ_hat = unflatten(λ_hat_vec)

    # Find hessian-vector product
    logger.debug(f"################## Find hessian-vector product...")
    primals_out, vjp_func = jax.vjp(F_fn, u, θ)
    _, λ_hat_i_dF_i_dθ_k = vjp_func(λ_hat) # λ_hat_i * (∂/∂u_k)F_i, λ_hat_i * (∂/∂θ_k)F_i

    dθ_dθ_J_θ_hat = jax.tree_util.tree_map(lambda x1, x2, x3, x4, x5: x1 + x2 + x3 + x4 + x5,
                                           dθ_k_du_j_J_u_hat_j, 
                                           dθ_k_dθ_j_J_θ_hat_j, 
                                           dθ_k_du_j_F_i_λ_i_u_hat_j, 
                                           dθ_k_dθ_j_F_i_λ_i_θ_hat_j,
                                           λ_hat_i_dF_i_dθ_k)

    logger.debug(f"################## Finshed using AD to find HVP.\n")

    profile_info = [J_time, F_time]

    return dθ_dθ_J_θ_hat, profile_info
