"""
This module implements the methods for calculating the plasma wakefields
using the 2D r-z reduced model from P. Baxevanis and G. Stupakov.

See https://journals.aps.org/prab/abstract/10.1103/PhysRevAccelBeams.21.071301
for the full details about this model.
"""
import copy
import numpy as np
import scipy.constants as ct
import aptools.plasma_accel.general_equations as ge
import matplotlib.pyplot as plt

from wake_t.utilities.other import radial_gradient
from .plasma_particles import PlasmaParticles
from wake_t.utilities.numba import njit_serial
from wake_t.particles.deposition import deposit_3d_distribution


def calculate_wakefields(laser_a2, bunches, r_max, xi_min, xi_max,
                         n_r, n_xi, ppc, n_p, r_max_plasma=None,
                         parabolic_coefficient=0., p_shape='cubic',
                         max_gamma=10., plasma_pusher='rk4',
                         ion_motion=False, fld_arrays=[]):
    """
    Calculate the plasma wakefields generated by the given laser pulse and
    electron beam in the specified grid points.

    Parameters
    ----------
    laser_a2 : ndarray
        A (nz x nr) array containing the square of the laser envelope.
    beam_part : list
        List of numpy arrays containing the spatial coordinates and charge of
        all beam particles, i.e [x, y, xi, q].
    r_max : float
        Maximum radial position up to which plasma wakefield will be
        calculated.
    xi_min : float
        Minimum longitudinal (speed of light frame) position up to which
        plasma wakefield will be calculated.
    xi_max : float
        Maximum longitudinal (speed of light frame) position up to which
        plasma wakefield will be calculated.
    n_r : int
        Number of grid elements along r in which to calculate the wakefields.
    n_xi : int
        Number of grid elements along xi in which to calculate the wakefields.
    ppc : int (optional)
        Number of plasma particles per 1d cell along the radial direction.
    n_p : float
        Plasma density in units of m^{-3}.
    r_max_plasma : float
        Maximum radial extension of the plasma column. If `None`, the plasma
        extends up to the `r_max` boundary of the simulation box.
    parabolic_coefficient : float
        The coefficient for the transverse parabolic density profile. The
        radial density distribution is calculated as
        `n_r = n_p * (1 + parabolic_coefficient * r**2)`, where `n_p` is the
        local on-axis plasma density.
    p_shape : str
        Particle shape to be used for the beam charge deposition. Possible
        values are 'linear' or 'cubic'.
    max_gamma : float
        Plasma particles whose `gamma` exceeds `max_gamma` are considered to
        violate the quasistatic condition and are put at rest (i.e.,
        `gamma=1.`, `pr=pz=0.`).
    plasma_pusher : str
        Numerical pusher for the plasma particles. Possible values are `'rk4'`
        and `'ab5'`.

    """
    rho, chi, E_r, E_z, B_t, xi_fld, r_fld = fld_arrays

    s_d = ge.plasma_skin_depth(n_p * 1e-6)
    r_max = r_max / s_d
    xi_min = xi_min / s_d
    xi_max = xi_max / s_d
    dr = r_max / n_r
    dxi = (xi_max - xi_min) / (n_xi - 1)
    parabolic_coefficient = parabolic_coefficient * s_d**2

    # Maximum radial extent of the plasma.
    if r_max_plasma is None:
        r_max_plasma = r_max
    else:
        r_max_plasma = r_max_plasma / s_d

    # Field node coordinates.
    r_fld = r_fld / s_d
    xi_fld = xi_fld / s_d
    log_r_fld = np.log(r_fld)

    # Initialize field arrays, including guard cells.
    a2 = np.zeros((n_xi+4, n_r+4))
    nabla_a2 = np.zeros((n_xi+4, n_r+4))
    psi = np.zeros((n_xi+4, n_r+4))
    W_r = np.zeros((n_xi+4, n_r+4))
    b_t_bar = np.zeros((n_xi+4, n_r+4))

    # Laser source.
    a2[2:-2, 2:-2] = laser_a2
    nabla_a2[2:-2, 2:-2] = radial_gradient(laser_a2, dr)

    # Beam source. This code is needed while no proper support particle
    # beams as input is implemented.
    b_t_beam = np.zeros((n_xi+4, n_r+4))
    for bunch in bunches:
        calculate_beam_source(bunch, n_p, n_r, n_xi, r_fld[0], xi_fld[0],
                              dr, dxi, p_shape, b_t_beam)
    
    # Calculate plasma response (including density, susceptibility, potential
    # and magnetic field)
    calculate_plasma_response(
        r_max, r_max_plasma, parabolic_coefficient, dr, ppc, n_r,
        plasma_pusher, p_shape, max_gamma, ion_motion, n_xi, a2, nabla_a2,
        b_t_beam, r_fld, log_r_fld, psi, b_t_bar, rho, chi, dxi
    )

    # Calculate derived fields (E_z, W_r, and E_r).
    E_0 = ge.plasma_cold_non_relativisct_wave_breaking_field(n_p*1e-6)
    dxi_psi, dr_psi = np.gradient(psi[2:-2, 2:-2], dxi, dr, edge_order=2)
    E_z[2:-2, 2:-2] = -dxi_psi * E_0
    W_r[2:-2, 2:-2] = -dr_psi * E_0
    B_t[:] = (b_t_bar + b_t_beam) * E_0 / ct.c
    E_r[:] = W_r + B_t * ct.c


@njit_serial()
def calculate_plasma_response(
    r_max, r_max_plasma, parabolic_coefficient, dr, ppc, n_r,
    plasma_pusher, p_shape, max_gamma, ion_motion, n_xi, a2,
    nabla_a2, b_t_beam, r_fld, log_r_fld, psi, b_t_bar, rho,
    chi, dxi
):
    # Initialize plasma particles.
    pp = PlasmaParticles(
        r_max, r_max_plasma, parabolic_coefficient, dr, ppc, n_r,
        max_gamma, ion_motion, plasma_pusher, p_shape
    )
    pp.initialize()

    # Evolve plasma from right to left and calculate psi, b_t_bar, rho and
    # chi on a grid.
    for step in range(n_xi):
        slice_i = n_xi - step - 1

        pp.sort()

        pp.determine_neighboring_points()

        pp.gather_sources(
            a2[slice_i+2], nabla_a2[slice_i+2],
            b_t_beam[slice_i+2], r_fld[0], r_fld[-1], dr
        )

        pp.calculate_fields()

        pp.calculate_psi_at_grid(r_fld, log_r_fld, psi[slice_i+2, 2:-2])
        pp.calculate_b_theta_at_grid(r_fld, b_t_bar[slice_i+2, 2:-2])

        pp.deposit_rho(rho[slice_i+2], r_fld, n_r, dr)
        pp.deposit_chi(chi[slice_i+2], r_fld, n_r, dr)

        pp.ions_computed = True

        if slice_i > 0:
            pp.evolve(dxi)


def calculate_beam_source(
        bunch, n_p, n_r, n_xi, r_min, xi_min, dr, dxi, p_shape, b_t):
    """
    Return a (nz+4, nr+4) array with the azimuthal magnetic field
    from a particle distribution. This is Eq. (18) in the original paper.

    """
    # Plasma skin depth.
    s_d = ge.plasma_skin_depth(n_p / 1e6)

    # Get and normalize particle coordinate arrays.
    xi_n = bunch.xi / s_d
    x_n = bunch.x / s_d
    y_n = bunch.y / s_d

    # Calculate particle weights.
    w = bunch.q / ct.e / (2 * np.pi * dr * dxi * s_d ** 3 * n_p)

    # Obtain charge distribution (using cubic particle shape by default).
    q_dist = np.zeros((n_xi + 4, n_r + 4))
    deposit_3d_distribution(xi_n, x_n, y_n, w, xi_min, r_min, n_xi, n_r, dxi,
                            dr, q_dist, p_shape=p_shape, use_ruyten=True)

    # Remove guard cells.
    q_dist = q_dist[2:-2, 2:-2]

    # Radial position of grid points.
    r_grid_g = (0.5 + np.arange(n_r)) * dr

    # At each grid cell, calculate integral only until cell center by
    # assuming that half the charge is evenly distributed within the cell
    # (i.e., subtract half the charge)
    subs = q_dist / 2

    # At the first grid point along r, subtract an additional 1/4 of the
    # charge. This comes from assuming that the density has to be zero on axis.
    subs[:, 0] += q_dist[:, 0]/4

    # Calculate field by integration.
    b_t[2:-2, 2:-2] += (
        (np.cumsum(q_dist, axis=1) - subs) * dr / np.abs(r_grid_g))

    return b_t
