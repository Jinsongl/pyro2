from __future__ import print_function

import importlib

import numpy as np
import matplotlib.pyplot as plt

import mhd.eos as eos
import mhd.derives as derives
import mhd.unsplit_fluxes as flx
import mesh.boundary as bnd
import mesh.patch as patch
from simulation_null import NullSimulation, grid_setup, bc_setup
import util.plot_tools as plot_tools
import particles.particles as particles
import mhd.mhd_integration as integration


class Variables(object):
    """
    a container class for easy access to the different mhd
    variable by an integer key
    """

    def __init__(self, ccd):
        self.nvar = len(ccd.names)

        # conserved variables -- we set these when we initialize for
        # they match the CellCenterData2d object.  Here, we only
        # worry about the cell-centered data
        self.idens = ccd.names.index("density")
        self.ixmom = ccd.names.index("x-momentum")
        self.iymom = ccd.names.index("y-momentum")
        self.iener = ccd.names.index("energy")
        self.ixmag = ccd.names.index("x-magnetic-field")
        self.iymag = ccd.names.index("y-magnetic-field")

        # if there are any additional variable, we treat them as
        # passively advected scalars
        self.naux = self.nvar - 6
        if self.naux > 0:
            self.irhox = 6
        else:
            self.irhox = -1

        # primitive variables -- these are all cell centered
        self.nq = 6 + self.naux

        self.irho = 0
        self.iu = 1
        self.iv = 2
        self.ip = 3
        self.ibx = 4
        self.iby = 5

        if self.naux > 0:
            self.ix = 6   # advected scalar
        else:
            self.ix = -1


def cons_to_prim(U, gamma, ivars, myg):
    """ convert an input vector of conserved variables to primitive variables """

    smallrho = 1.e-10
    smallp = 1.e-10

    q = myg.scratch_array(nvar=ivars.nq)

    U[:, :, ivars.idens] = np.maximum(U[:, :, ivars.idens], smallrho)

    q[:, :, ivars.irho] = np.maximum(U[:, :, ivars.idens], smallrho)
    q[:, :, ivars.iu] = U[:, :, ivars.ixmom] / q[:, :, ivars.irho]
    q[:, :, ivars.iv] = U[:, :, ivars.iymom] / q[:, :, ivars.irho]
    q[:, :, ivars.ibx] = U[:, :, ivars.ixmag]
    q[:, :, ivars.iby] = U[:, :, ivars.iymag]

    e = (U[:, :, ivars.iener] -
         0.5 * q[:, :, ivars.irho] * (q[:, :, ivars.iu]**2 +
                                      q[:, :, ivars.iv]**2) -
         0.5 * (q[:, :, ivars.ibx]**2 + q[:, :, ivars.iby]**2)) / q[:, :, ivars.irho]

    q[:, :, ivars.ip] = np.maximum(
        smallp, eos.pres(gamma, q[:, :, ivars.irho], e))

    if ivars.naux > 0:
        for nq, nu in zip(range(ivars.ix, ivars.ix + ivars.naux),
                          range(ivars.irhox, ivars.irhox + ivars.naux)):
            q[:, :, nq] = U[:, :, nu] / q[:, :, ivars.irho]

    return q


def prim_to_cons(q, gamma, ivars, myg):
    """ convert an input vector of primitive variables to conserved variables """

    smallrho = 1.e-10
    smallp = 1.e-10

    U = myg.scratch_array(nvar=ivars.nvar)

    U[:, :, ivars.idens] = np.maximum(q[:, :, ivars.irho], smallrho)
    U[:, :, ivars.ixmom] = q[:, :, ivars.iu] * U[:, :, ivars.idens]
    U[:, :, ivars.iymom] = q[:, :, ivars.iv] * U[:, :, ivars.idens]
    U[:, :, ivars.ixmag] = q[:, :, ivars.ibx]
    U[:, :, ivars.iymag] = q[:, :, ivars.iby]

    rhoe = eos.rhoe(gamma, np.maximum(smallp, q[:, :, ivars.ip]))

    U[:, :, ivars.iener] = rhoe + 0.5 * q[:, :, ivars.irho] * \
        (q[:, :, ivars.iu]**2 + q[:, :, ivars.iv]**2) + \
        0.5 * (q[:, :, ivars.ibx]**2 + q[:, :, ivars.iby]**2)

    if ivars.naux > 0:
        for nq, nu in zip(range(ivars.ix, ivars.ix + ivars.naux),
                          range(ivars.irhox, ivars.irhox + ivars.naux)):
            U[:, :, nu] = q[:, :, nq] * q[:, :, ivars.irho]

    return U


class Simulation(NullSimulation):
    """The main simulation class for the corner transport upwind
    mhd hydrodynamics solver

    """

    def initialize(self, extra_vars=None, ng=4):
        """
        Initialize the grid and variables for mhd flow and set
        the initial conditions for the chosen problem.
        """
        my_grid = grid_setup(self.rp, ng=ng)
        my_data = self.data_class(my_grid)

        bc, bc_xodd, bc_yodd = bc_setup(self.rp)

        # are we dealing with solid boundaries? we'll use these for
        # the Riemann solver
        self.solid = bnd.bc_is_solid(bc)

        # density and energy
        my_data.register_var("density", bc)
        my_data.register_var("x-momentum", bc_xodd)
        my_data.register_var("y-momentum", bc_yodd)
        my_data.register_var("energy", bc)
        my_data.register_var("x-magnetic-field", bc)
        my_data.register_var("y-magnetic-field", bc)

        # any extras?
        if extra_vars is not None:
            for v in extra_vars:
                my_data.register_var(v, bc)

        # store the EOS gamma as an auxillary quantity so we can have a
        # self-contained object stored in output files to make plots.
        # store grav because we'll need that in some BCs
        my_data.set_aux("gamma", self.rp.get_param("eos.gamma"))

        my_data.create()

        self.cc_data = my_data

        # we also need face-centered data for the magnetic fields
        fcx = patch.FaceCenterData2d(my_grid, 1)
        fcx.register_var("x-magnetic-field", bc)
        fcx.create()
        self.fcx_data = fcx

        fcy = patch.FaceCenterData2d(my_grid, 2)
        fcy.register_var("y-magnetic-field", bc)
        fcy.create()
        self.fcy_data = fcy

        if self.rp.get_param("particles.do_particles") == 1:
            self.particles = particles.Particles(self.cc_data, bc, self.rp)

        self.ivars = Variables(my_data)

        # derived variables
        self.cc_data.add_derived(derives.derive_primitives)

        # initial conditions for the problem
        problem = importlib.import_module("{}.problems.{}".format(
            self.solver_name, self.problem_name))
        problem.init_data(self.cc_data, self.fcx_data, self.fcy_data, self.rp)

        self.cc_data.fill_BC_all()
        self.fcx_data.fill_BC_all()
        self.fcy_data.fill_BC_all()

        if self.verbose > 0:
            print(my_data)

    def method_compute_timestep(self):
        """
        The timestep function computes the advective timestep (CFL)
        constraint.  The CFL constraint says that information cannot
        propagate further than one zone per timestep.

        We use the driver.cfl parameter to control what fraction of the
        CFL step we actually take.
        """

        cfl = self.rp.get_param("driver.cfl")
        fix_dt = self.rp.get_param("driver.fix_dt")

        # get the variables we need
        u, v = self.cc_data.get_var("velocity")
        Cfx, _, Cfy, _ = self.cc_data.get_var(
            ["x-magnetosonic", "y-magnetosonic"])

        # a = self.cc_data.get_var("soundspeed")
        # print(f"fast magnetosonic speeds = {Cfx.max()}, {Cfy.max()}")
        # print(f"a = {a.max()}")
        # print(f"u, v = {abs(u).max(), abs(v).max()}")

        # the timestep is min(dx/(|u| + cs), dy/(|v| + cs))
        xtmp = self.cc_data.grid.dx / (abs(u) + Cfx)
        ytmp = self.cc_data.grid.dy / (abs(v) + Cfy)

        self.dt = cfl * min(xtmp.min(), ytmp.min(), fix_dt)

    def substep(self, cc_data, fcx_data, fcy_data):

        return flx.timestep(cc_data, fcx_data, fcy_data,
                            self.rp,
                            self.ivars, self.solid, self.tc, self.dt)

    def evolve(self):
        """
        Evolve the equations of mhd hydrodynamics through a
        timestep dt.
        """

        tm_evolve = self.tc.timer("evolve")
        tm_evolve.begin()

        myd = self.cc_data
        method = self.rp.get_param("mhd.temporal_method")

        rk = integration.RKIntegratorMHD(myd.t, self.dt, method=method)
        rk.set_start((myd, self.fcx_data, self.fcy_data))

        for s in range(rk.nstages()):
            ytmp, fxtmp, fytmp = rk.get_stage_start(s)
            ytmp.fill_BC_all()
            fxtmp.fill_BC_all()
            fytmp.fill_BC_all()
            k, kx, ky = self.substep(ytmp, fxtmp, fytmp)
            rk.store_increment(s, k, kx, ky)

        rk.compute_final_update()

        # update the particles

        if self.particles is not None:
            self.particles.update_particles(self.dt)

        ########################################################################
        # STEP 10. increment the time
        ########################################################################

        # fill the ghost cells
        self.cc_data.fill_BC_all()
        self.fcx_data.fill_BC_all()
        self.fcy_data.fill_BC_all()

        self.method_compute_timestep()
        self.cc_data.t += self.dt
        self.n += 1

        tm_evolve.end()

    def dovis(self):
        """
        Do runtime visualization.
        """

        plt.clf()

        plt.rc("font", size=10)

        # we do this even though ivars is in self, so this works when
        # we are plotting from a file
        ivars = Variables(self.cc_data)

        # access gamma from the cc_data object so we can use dovis
        # outside of a running simulation.
        gamma = self.cc_data.get_aux("gamma")

        q = cons_to_prim(self.cc_data.data, gamma, ivars, self.cc_data.grid)

        rho = q[:, :, ivars.irho]
        u = q[:, :, ivars.iu]
        v = q[:, :, ivars.iv]
        p = q[:, :, ivars.ip]
        e = eos.rhoe(gamma, p) / rho
        bx = q[:, :, ivars.ibx]
        by = q[:, :, ivars.iby]

        magvel = np.sqrt(u**2 + v**2)
        magb = np.sqrt(bx**2 + by**2)

        # print(magb[magb > 0])

        myg = self.cc_data.grid

        fields = [rho, magvel, e, magb]
        field_names = [r"$\rho$", r"|U|", "e", "|B|"]

        _, axes, cbar_title = plot_tools.setup_axes(myg, len(fields))

        for n, ax in enumerate(axes):
            v = fields[n]

            img = ax.imshow(np.transpose(v.v()),
                            interpolation="nearest", origin="lower",
                            extent=[myg.xmin, myg.xmax, myg.ymin, myg.ymax],
                            cmap=self.cm)

            ax.set_xlabel("x")
            ax.set_ylabel("y")

            # needed for PDF rendering
            cb = axes.cbar_axes[n].colorbar(img)
            cb.solids.set_rasterized(True)
            cb.solids.set_edgecolor("face")

            if cbar_title:
                cb.ax.set_title(field_names[n])
            else:
                ax.set_title(field_names[n])

        if self.particles is not None:
            ax = axes[0]
            particle_positions = self.particles.get_positions()
            # dye particles
            colors = self.particles.get_init_positions()[:, 0]

            # plot particles
            ax.scatter(particle_positions[:, 0],
                       particle_positions[:, 1], s=5, c=colors, alpha=0.8, cmap="Greys")
            ax.set_xlim([myg.xmin, myg.xmax])
            ax.set_ylim([myg.ymin, myg.ymax])

        plt.figtext(0.05, 0.0125, "t = {:10.5g}".format(self.cc_data.t))

        plt.pause(0.001)
        plt.draw()

    def write_extras(self, f):
        """
        Output simulation-specific data to the h5py file f
        """

        # make note of the custom BC
        gb = f.create_group("BC")

        # the value here is the value of "is_solid"
        gb.create_dataset("hse", data=False)