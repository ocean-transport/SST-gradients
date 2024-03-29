import xarray as xr
import numpy as np
import dask.array as dsa
import matplotlib.pyplot as plt
from fastjmd95 import jmd95numba
from intake import open_catalog
import logging

# (Compute the gradient https://pop-tools.readthedocs.io/en/latest/examples/pop_div_curl_xr_xgcm_metrics_compare.html#gradient)

#ignore Runtimewarning
np.seterr(divide='ignore', invalid='ignore')

#create a cluster
from dask_gateway import Gateway
from dask.distributed import Client

#g = Gateway()
#c = g.list_clusters()[0]
#cluster = g.connect(c.name)
#client = Client(cluster)
#client

gateway = Gateway()
options = gateway.cluster_options()
options.worker_memory=20
cluster = gateway.new_cluster(options)
cluster.adapt(minimum=1,maximum=30)
client = Client(cluster)
print(client)

#use logger to show progress
logging.basicConfig(level=logging.INFO)

logging.info('loading in data')

#call the data
url = "https://raw.githubusercontent.com/pangeo-data/pangeo-datastore/master/intake-catalogs/ocean/CESM_POP.yaml"
cat = open_catalog(url)
ds  = cat["CESM_POP_hires_control"].to_dask()

#select timestamp
t = 0

logging.info("starting Paige's code")
#from Paige's code: The biharmonic horiz diffusion routine
#https://github.com/ocean-transport/cesm-air-sea/blob/master/biharmonic_tendency.ipynb

# raw grid geometry
work1 = (ds['HTN'].values / # HTN: cell widths on North sides of T cell (cm)
         ds['HUW'].values) # HUW: cell widths on West sides of U cell (cm)
tarea = ds['TAREA'].values # TAREA: area of T cells (cm**2)
tarea_r = np.ma.masked_invalid(tarea**-1).filled(0.) # 1/area of T cells
dtn = work1*tarea_r # coefficient of north point in 5-point stencil
dts = np.roll(work1,-1,axis=0)*tarea_r # coeff of south point in 5-point stencil

work1 = (ds['HTE'].values / # HTE: cell widths on East sides of T cells (cm)
         ds['HUS'].values) # HUS: cell widths on South sides of U cells (cm)
dte = work1*tarea_r # coeff of east point in 5-point stencil
dtw = np.roll(work1,-1,axis=1)*tarea_r # coeff of west point in 5-point stencil

kmt = ds['KMT'].values # KMT: k-index of deepest grid cell on T grid (where k is the depth level)

logging.info('setting bc')
# boundary conditions
kmt_ = kmt > 1 # k=1 is the surface, so this sets all subsurface levels to True
kmtn = np.roll(kmt_,-1,axis=0)
kmts = np.roll(kmt_,1,axis=0)
kmte = np.roll(kmt_,-1,axis=1)
kmtw = np.roll(kmt_,1,axis=1)
cn = np.where(kmt_ & kmtn, dtn, 0.) # where both kmt_ and kmtn are True, set value equal to dtn, else set to 0 -->
# --> essentially sets up a mask for land/surface points
cs = np.where(kmt_ & kmts, dts, 0.)
ce = np.where(kmt_ & kmte, dte, 0.)
cw = np.where(kmt_ & kmtw, dtw, 0.)

# Find single index where there's a min of squared latitude --> so this is probably the lat of equator
j_eq = np.argmin(ds['ULAT'].values[:,0]**2) # ULAT: array of U-grid latitudes (degrees North)
j_eq.shape

# Area of T cells / area of U cell at the equator, all raised to 1.5 power
# this is because the mixing scheme assumes the grid spacing that is at the equator, so coeffs are 1 at equator and reduce toward the poles
ahf = (tarea / ds['UAREA'].values[j_eq, 0])**1.5 # UAREA: area of U cells (cm**2)
ahf[kmt <= 1] = 0.

logging.info('laplacian operator define')
def laplacian(T, cn, cs, ce, cw):
    cc = -(cn + cs + ce + cw) # cn,cs,ce,cw are coeffs for laplacian
    return (
        cc * T +
        cn * np.roll(T, -1, axis=-2) +
        cs * np.roll(T, 1, axis=-2) +
        ce * np.roll(T, -1, axis=-1) +
        cw * np.roll(T, 1, axis=-1)          
    )

logging.info('biharmonic operator defined')
def biharmonic_tendency(T, ahf, cn, cs, ce, cw):
    ah=-3e17 # horizontal tracer mixing coefficient 
    d2tk = ahf * laplacian(T, cn, cs, ce, cw) # take laplacian of T, multiplying by grid factor due to equator
    return ah * laplacian(d2tk, cn, cs, ce, cw) # take laplacian of laplacian of T

#logging.info('futures defined')
#futures = client.scatter([ahf, cn, cs, ce, cw], broadcast=True) # this sends all these coeffs to all workers in distributed memory
#END OF PAIGE'S CODE

logging.info('creating t/s tendecies')
#create temp/salt tendency from mixing [M(temp), M(S) in EOS eqs section]
SST_bih = xr.DataArray(dsa.map_blocks(biharmonic_tendency, ds.SST.data, ahf, cn, cs, ce, cw, 
                                      dtype=ds.SST.data.dtype),
                       dims=ds.SST.dims,
                       coords=ds.SST.reset_coords(drop=True).coords)
SSS_bih = xr.DataArray(dsa.map_blocks(biharmonic_tendency, ds.SSS.data, ahf, cn, cs, ce, cw, 
                                      dtype=ds.SSS.data.dtype),
                       dims=ds.SSS.dims,
                       coords=ds.SSS.reset_coords(drop=True).coords)

logging.info('convert to density tendency')
#convert to density tendency [alpha*M(temp) + beta*M(S)]

#for a single timestep to save computation cost
sst = ds.SST.isel(time=t)
sss = ds.SSS.isel(time=t)

#runit2mass = 1.035e3 #rho_0

logging.info('define drhods, drhodt')
drhodt = xr.apply_ufunc(jmd95numba.drhodt, sss, sst, 0,
                        output_dtypes=[sst.dtype],
                        dask='parallelized').reset_coords(drop=True)#.load()
drhods = xr.apply_ufunc(jmd95numba.drhods, sss, sst, 0,
                        output_dtypes=[sss.dtype],
                        dask='parallelized').reset_coords(drop=True)#.load()

#alpha = - drhodt / runit2mass
#beta = drhods / runit2mass

dens_tend = drhodt * SST_bih.isel(time=t) + drhods * SSS_bih.isel(time=t)
dens_tend

logging.info('calculate M(rho)')
#calculate M(rho)
rho = xr.apply_ufunc(jmd95numba.rho, ds.SSS, ds.SST, 0,
                        output_dtypes=[ds.SST.dtype],
                        dask='parallelized').reset_coords(drop=True)#.load()
rho_bih = xr.DataArray(dsa.map_blocks(biharmonic_tendency, rho.data, ahf, cn, cs, ce, cw, 
                                      dtype=rho.data.dtype),
                       dims=rho.dims,
                       coords=rho.reset_coords(drop=True).coords)

logging.info('calculate cabbeling term')
#determine cabbeling as C = alpha*M(temp) + beta*M(S) - M(rho)
cabbeling = dens_tend - rho_bih

logging.info('plotting all four terms')
#plot all four terms
selection = dict(time=0, nlat=slice(1500,1600), nlon=slice(500,600))
kwargs = {'shrink': 0.8, 'label':r'[$\frac{kg}{m^3 s}$]'}

fig, ax = plt.subplots(2,2, figsize=(15,10))

(SST_bih*drhodt).isel(**selection).plot(robust=True, ax=ax[0,0], 
                                 cbar_kwargs=kwargs)
ax[0,0].set_title('SST mixing tendency')
(SSS_bih*drhods).isel(**selection).plot(robust=True, ax=ax[0,1], 
                                 cbar_kwargs=kwargs)
ax[0,1].set_title('SSS mixing tendency')
(rho_bih).isel(**selection).plot(robust=True, ax=ax[1,0], 
                                 cbar_kwargs=kwargs)
ax[1,0].set_title(r'$\rho$ mixing tendency')
(cabbeling).isel(**selection).plot(robust=True, ax=ax[1,1], 
                                   cbar_kwargs=kwargs)
ax[1,1].set_title('Cabbeling tendency')

plt.tight_layout()
plt.savefig('tendency_terms.png');

logging.info('Make histogram of each term')
#histogram of each term

vol = ds.TAREA * ds.dz

from xhistogram.xarray import histogram

delta_rho = 0.01
rho_bins = np.arange(1015, 1030, delta_rho)

tendency_terms = xr.merge([(SST_bih*drhodt).rename('sst'), (SSS_bih*drhods).rename('sss'),
                           rho_bih.rename('rho'), cabbeling.rename('cabbeling')])
all_tendencies = list(tendency_terms)

print('making histogram func to run on the 4 tendencies')

def histogram_func(variable):
    """Generalized xhistogram's histogram function
    for mixing tendency terms"""
    hist = histogram(rho.rename('rho0'), bins=[rho_bins],
                     weights=variable.fillna(0.), dim=['nlon', 'nlat'])
    return hist / (-delta_rho)

print('running func on all_tendencies')

all_dsets = xr.merge([histogram_func(tendency_terms[var]).rename('OMEGA_' + var)
                      for var in all_tendencies])
print(all_dsets)

#plot all four terms' transformation
print('plotting transformation terms')
kwargs = {'shrink': 0.8, 'label':r'[$\frac{kg}{m^3 s}$]'}

fig, ax = plt.subplots(2,2, figsize=(15,10))

(all_dsets.OMEGA_sst).plot(robust=True, ax=ax[0,0],
                                 cbar_kwargs=kwargs)
ax[0,0].set_title('SST transformation')
(all_dsets.OMEGA_sss).plot(robust=True, ax=ax[0,1],
                                 cbar_kwargs=kwargs)
ax[0,1].set_title('SSS transformation')
(all_dsets.OMEGA_rho).plot(robust=True, ax=ax[1,0],
                                 cbar_kwargs=kwargs)
ax[1,0].set_title(r'$\rho$ transformation')
(all_dsets.OMEGA_cabbeling).plot(robust=True, ax=ax[1,1],
                                   cbar_kwargs=kwargs)
ax[1,1].set_title('Cabbeling transformation')

plt.tight_layout()
plt.savefig('tendency_terms_transformation.png');
